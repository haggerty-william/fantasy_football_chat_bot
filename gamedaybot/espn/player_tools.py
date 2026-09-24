"""Bounded player discovery and NFL evidence from fixed ESPN read endpoints.

The SDK's player map and field mappings identify players; explicit HTTP reads
mirror its player-card/free-agent filters without its unbounded request timeout.
Current ownership/status and season aggregates never explain a historical week.
"""
from datetime import datetime, timezone
import json
import re
import time
from types import SimpleNamespace

from espn_api.football.constant import PLAYER_STATS_MAP, POSITION_MAP, PRO_TEAM_MAP

from gamedaybot.espn.decision_tools import tool
from gamedaybot.espn.research import normalized, number, relevant_news


FANTASY = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/'
SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary'
POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'D/ST', 'FLEX', 'DT', 'DE', 'LB', 'DL', 'CB', 'S', 'DB', 'DP']
IDS = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3,
       'description': 'Exact ESPN IDs from rosters or search/free-agent discovery results.'}
TOOLS = [
    tool('search_players', 'Find ESPN players by name, including players outside league rosters. Returns exact IDs for other player tools; current ownership is omitted for historical reports.',
         {'query': {'type': 'string', 'minLength': 2, 'maxLength': 80},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, ['query']),
    tool('get_player_season_details', 'Expand known players into observed season and weekly stats/projections, position eligibility and ESPN-wide ownership. Historical reports exclude later weeks, current status/ownership and season totals. Projections are fetched now, not archived forecasts.',
         {'player_ids': IDS}, ['player_ids']),
    tool('get_free_agent_pool', 'Browse a bounded page of current FREEAGENT or WAIVERS players, optionally by eligible position. Local ownership/projection filters apply to that page only. Does not claim players or prove a waiver can clear.',
         {'position': {'type': 'string', 'enum': POSITIONS},
          'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
          'max_percent_owned': {'type': 'number', 'minimum': 0, 'maximum': 100},
          'min_projected_points': {'type': 'number', 'minimum': -100, 'maximum': 100}}, []),
    tool('get_player_nfl_schedule', 'Look up upcoming NFL opponents and verified byes for known players using current NFL affiliation. No opponent difficulty is inferred; historical reports cannot use current team affiliation.',
         {'player_ids': IDS, 'start_week': {'type': 'integer', 'minimum': 1, 'maximum': 18},
          'weeks': {'type': 'integer', 'minimum': 1, 'maximum': 6}}, ['player_ids']),
    tool('get_nfl_scoreboard', 'Get the NFL regular-season scoreboard for one week in this league season, including event IDs for get_nfl_game_summary. Historical reports cannot inspect later weeks.',
         {'week': {'type': 'integer', 'minimum': 1, 'maximum': 18}}, []),
    tool('get_nfl_game_summary', 'Get concise NFL team statistics, leaders and recent scoring plays for an event returned by get_nfl_scoreboard. No articles or videos; NFL scores are not fantasy points.',
         {'event_id': {'type': 'string', 'pattern': '^[0-9]{1,12}$'}}, ['event_id']),
]
NAMES = {item['function']['name'] for item in TOOLS}


def _cache(researcher):
    if not hasattr(researcher, 'player_tools_cache'):
        researcher.player_tools_cache = {}
    if not hasattr(researcher, 'discovered_players'):
        researcher.discovered_players = {}
    return researcher.player_tools_cache


def _check_deadline(researcher):
    if time.monotonic() >= researcher.deadline - 2:
        raise ValueError('Research deadline exhausted')


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=120):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _historical(researcher):
    period = getattr(researcher.league, 'scoringPeriodId', None)
    return bool(researcher.context.get('historical') or (
        type(researcher.week) is int and type(period) is int and researcher.week < period))


def _year(researcher):
    year = getattr(researcher.league, 'year', None)
    if not _integer(year, 2019, 2100):
        raise ValueError('Unsupported league season')
    return year


def _period(researcher):
    period = getattr(researcher.league, 'scoringPeriodId', None)
    if not _integer(period, 1, 25):
        raise ValueError('Unknown scoring period')
    return period


def _week(researcher):
    week = researcher.week if researcher.week is not None else _period(researcher)
    if not _integer(week, 1, 25):
        raise ValueError('Unknown report week')
    return week


def _read_league(researcher, view, filters, params=None):
    from gamedaybot.espn.espn_read import read_league
    _check_deadline(researcher)
    key = ('fantasy', view, json.dumps(filters, sort_keys=True), json.dumps(params, sort_keys=True))
    cache = _cache(researcher)
    if key not in cache:
        cache[key] = read_league(researcher.league, [view], params=params, filters=filters,
                                 deadline=researcher.deadline, timeout=8, max_bytes=2_000_000)
    return cache[key]


def _read_public(researcher, url, params):
    from gamedaybot.espn.espn_read import read_espn
    _check_deadline(researcher)
    key = ('public', url, json.dumps(params, sort_keys=True))
    cache = _cache(researcher)
    if key not in cache:
        cache[key] = read_espn(url, params=params, deadline=researcher.deadline,
                               timeout=8, max_bytes=2_000_000)
    return cache[key]


def _numeric_stats(values):
    if not isinstance(values, dict):
        return {}
    result = {}
    for key, value in list(values.items())[:100]:
        stat = PLAYER_STATS_MAP.get(int(key), str(key)) if str(key).isdigit() else str(key)
        if number(value) is not None:
            result[str(stat)[:80]] = number(value)
    return result


def _parse_player(row, year):
    """Select only known fields, preserving missing points instead of SDK zeros."""
    entry = row.get('playerPoolEntry', row)
    raw = entry.get('player', {})
    pid = raw.get('id', entry.get('id'))
    if not _integer(pid, -9999999999, 9999999999) or not pid or not _text(raw.get('fullName')):
        return None
    stats = {}
    for item in (raw.get('stats') or [])[:100]:
        if (not isinstance(item, dict) or item.get('seasonId') != year or item.get('statSplitTypeId') == 2
                or item.get('statSourceId') not in (0, 1) or not _integer(item.get('scoringPeriodId'), 0, 25)):
            continue
        target = stats.setdefault(item['scoringPeriodId'], {})
        projected = item['statSourceId'] == 1
        for source, actual, forecast in (
                ('appliedTotal', 'points', 'projected_points'),
                ('appliedAverage', 'avg_points', 'projected_avg_points')):
            if number(item.get(source)) is not None:
                target[forecast if projected else actual] = number(item[source])
        target['projected_breakdown' if projected else 'breakdown'] = _numeric_stats(item.get('stats'))
        target['projected_points_breakdown' if projected else 'points_breakdown'] = _numeric_stats(item.get('appliedStats'))
    ownership = raw.get('ownership') or {}
    status = entry.get('status') or row.get('status')
    return SimpleNamespace(
        playerId=pid, name=_text(raw['fullName']), stats=stats,
        position=POSITION_MAP.get(raw.get('defaultPositionId')),
        eligibleSlots=[POSITION_MAP[p] for p in (raw.get('eligibleSlots') or [])[:30] if type(p) is int and p in POSITION_MAP],
        proTeam=PRO_TEAM_MAP.get(raw.get('proTeamId')), pro_team_id=raw.get('proTeamId'),
        injuryStatus=_text(raw.get('injuryStatus')) or 'UNKNOWN',
        onTeamId=entry.get('onTeamId'), availability=status if status in ('FREEAGENT', 'WAIVERS', 'ONTEAM') else None,
        percent_owned=number(ownership.get('percentOwned')),
        percent_started=number(ownership.get('percentStarted')), schedule={})


def _register(researcher, players):
    _cache(researcher)
    for player in players:
        pid = str(player.playerId)
        researcher.discovered_players[pid] = player
        researcher.allowed.add(pid)


def _cards(researcher, ids):
    cache = _cache(researcher)
    missing = [pid for pid in ids if ('card', pid) not in cache]
    if missing:
        filters = {'players': {'filterIds': {'value': [int(pid) for pid in missing]},
                   'filterStatsForTopScoringPeriodIds': {'value': 25,
                                                       'additionalValue': [f'00{_year(researcher)}', f'10{_year(researcher)}']}}}
        data = _read_league(researcher, 'kona_playercard', filters)
        found = {}
        for row in data.get('players', [])[:30]:
            player = _parse_player(row, _year(researcher))
            if player is not None and str(player.playerId) in missing:
                found[str(player.playerId)] = player
        for pid in missing:
            cache[('card', pid)] = found.get(pid)
        _register(researcher, found.values())
    return [cache[('card', pid)] for pid in ids if cache[('card', pid)] is not None]


def _identity(player, researcher):
    result = {'id': str(player.playerId), 'name': player.name, 'position': getattr(player, 'position', None)}
    if _historical(researcher):
        result['fantasy_team'] = 'Historical ownership unknown'
        return result
    result['nfl_team'] = getattr(player, 'proTeam', None)
    owner_id = getattr(player, 'onTeamId', None)
    teams = getattr(researcher.league, 'teams', [])
    owner = next((t for t in teams if owner_id is not None and str(t.team_id) == str(owner_id)), None)
    if owner_id is None:
        owner = next((t for t in teams if any(str(p.playerId) == str(player.playerId) for p in getattr(t, 'roster', []))), None)
    if owner is not None:
        result.update(team_id=str(owner.team_id), fantasy_team=owner.team_name)
    else:
        result['fantasy_team'] = 'Unrostered or on waivers' if owner_id == 0 else 'Current ownership unknown'
    return result


def _actual(row):
    return {k: row[k] for k in ('points', 'avg_points', 'breakdown', 'points_breakdown') if k in row}


def _forecast(row):
    return {k: row[k] for k in ('projected_points', 'projected_avg_points', 'projected_breakdown', 'projected_points_breakdown') if k in row}


def _metrics(values):
    # Only named NFL statistics; omit raw scoring IDs and duplicated applied
    # fantasy-point breakdowns. Prioritize nonzero fields within a fixed cap.
    meaningful = set(PLAYER_STATS_MAP.values())
    rows = [(key, value) for key, value in values.items() if key in meaningful]
    rows.sort(key=lambda pair: pair[1] == 0)
    return dict(rows[:12])


def _details(player, researcher, full=False):
    result = _identity(player, researcher)
    week, period = _week(researcher), _period(researcher)
    historical = _historical(researcher)
    stats = getattr(player, 'stats', {})
    row = stats.get(week, {})
    if week <= period:
        result.update({k: row[k] for k in ('points',) if k in row})
        result['stats'] = _metrics(row.get('breakdown', {}))
    if not historical:
        result.update(current_status=getattr(player, 'injuryStatus', None) or 'UNKNOWN',
                      eligible_slots=list(getattr(player, 'eligibleSlots', [])))
        result['availability'] = getattr(player, 'availability', None) or 'UNKNOWN'
        for attr, key in (('percent_owned', 'percent_owned'), ('percent_started', 'percent_started')):
            value = number(getattr(player, attr, None))
            result[key] = value if value is not None and 0 <= value <= 100 else None
        if 'projected_points' in row:
            result['projected_points'] = row['projected_points']
        result['ownership_scope'] = 'ESPN-wide percentages observed now, not league ownership or waiver priority.'
    result['week'] = week
    if full:
        weeks = []
        for w, source in sorted(stats.items()):
            if not _integer(w, 1, 25) or (historical and w > week):
                continue
            detail = {'week': w, 'scope': 'completed_week' if w < period else 'current_week_may_be_in_progress' if w == period else 'future_projection'}
            if w <= period and 'points' in source:
                detail['points'] = source['points']
            if not historical and 'projected_points' in source:
                detail['projected_points'] = source['projected_points']
            if len(detail) > 2:
                weeks.append(detail)
        result['weekly_stats'] = weeks
        if not historical and 0 in stats:
            result['season_observed'] = {key: stats[0][key] for key in ('points', 'avg_points') if key in stats[0]}
            result['season_observed']['stats'] = _metrics(stats[0].get('breakdown', {}))
            result['season_projection'] = {key: stats[0][key] for key in ('projected_points', 'projected_avg_points') if key in stats[0]}
            result['season_projection']['stats'] = _metrics(stats[0].get('projected_breakdown', {}))
        result['statistics_coverage'] = 'Weekly rows contain fantasy totals; up to 12 named football metrics describe the report week and each season aggregate. Missing or omitted metrics are unknown.'
        result['projections_scope'] = ('Historical projections omitted: no archived pregame forecast is available.' if historical else
            'ESPN projections fetched now, not archived pregame values; future actual points are omitted.')
    return result


def discovered_context(researcher, ids):
    """Compatible evidence for existing news/stats/status tools on discovered IDs.

    The dispatcher merges this with roster research; it should not replace a
    historical lineup's ownership/slot with the player's current NFL/fantasy team.
    """
    _check_deadline(researcher)
    _cache(researcher)
    players = [researcher.discovered_players[pid] for pid in ids if pid in researcher.discovered_players]
    packet = {'source': 'ESPN Fantasy', 'fetched_at': _stamp(), 'week': _week(researcher),
              'players': [_details(p, researcher) for p in players], 'news': [],
              'limitations': ['Current ESPN status is not proof of health; missing evidence is unknown.',
                              'Discovered players have no verified lineup slot in this packet.']}
    if not _historical(researcher) and players and researcher.deadline - time.monotonic() >= 15:
        key = ('discovered_news', tuple(sorted(str(p.playerId) for p in players)))
        cache = _cache(researcher)
        if key not in cache:
            try:
                cache[key] = relevant_news([p.name for p in players], datetime.now(timezone.utc))
            except Exception:
                cache[key] = ([], None)
        packet['news'], packet['news_fetched_at'] = cache[key]
        packet['news_status'] = 'Available' if packet['news_fetched_at'] else 'Unavailable'
    elif not _historical(researcher) and players:
        packet['news_status'] = 'Unavailable within remaining research budget'
    return packet


def _search(args, researcher):
    query = normalized(args['query'])
    candidates = []
    for pid, name in list(getattr(researcher.league, 'player_map', {}).items())[:20000]:
        if type(pid) is not int or not isinstance(name, str):
            continue
        label = normalized(name)
        if all(part in label for part in query.split()):
            candidates.append((label != query, not label.startswith(query), label, str(pid)))
    candidates.sort()
    limit = args.get('limit', 5)
    ids = [row[3] for row in candidates[:limit]]
    players = _cards(researcher, ids) if ids else []
    return {'players': [_identity(p, researcher) for p in players], 'matched_names': len(candidates),
            'returned': len(players), 'omitted_matches': max(0, len(candidates) - limit),
            'limitations': ['Search uses ESPN active-player names loaded with this league snapshot; retired/inactive players may be absent. Partial names can match several players; use returned exact IDs.']}


def _free_agents(args, researcher):
    if _historical(researcher):
        return {'error': 'Current free-agent availability cannot explain a historical report.'}
    if _week(researcher) != _period(researcher):
        return {'error': 'Free-agent availability is supported only for the current scoring week.'}
    offset, limit = args.get('offset', 0), args.get('limit', 10)
    filters = {'players': {'filterStatus': {'value': ['FREEAGENT', 'WAIVERS']},
               'filterSlotIds': {'value': [POSITION_MAP[args['position']]] if 'position' in args else []},
               'offset': offset, 'limit': limit,
               'sortPercOwned': {'sortPriority': 1, 'sortAsc': False},
               'sortDraftRanks': {'sortPriority': 100, 'sortAsc': True, 'value': 'STANDARD'}}}
    data = _read_league(researcher, 'kona_player_info', filters, {'scoringPeriodId': _week(researcher)})
    rows = data.get('players', [])[:limit]
    players = [p for row in rows if (p := _parse_player(row, _year(researcher))) is not None
               and p.onTeamId in (None, 0) and p.availability != 'ONTEAM']
    # The endpoint's availability filter is authoritative, even if no status
    # field is present. Do not turn this into a claim that waivers are cleared.
    for player in players:
        player.onTeamId = 0
    _register(researcher, players)
    details = [_details(p, researcher) for p in players]
    if 'max_percent_owned' in args:
        details = [p for p in details if p.get('percent_owned') is not None and p['percent_owned'] <= args['max_percent_owned']]
    if 'min_projected_points' in args:
        details = [p for p in details if p.get('projected_points') is not None and p['projected_points'] >= args['min_projected_points']]
    return {'players': details, 'offset': offset, 'scanned': len(rows), 'returned': len(details),
            'next_offset': offset + limit if len(rows) == limit and offset + limit <= 100 else None,
            'partial_coverage': True, 'sort': 'ESPN-wide ownership descending, then ESPN draft rank',
            'limitations': ['Only this bounded page was searched. Filters apply within the page, not the whole player pool; rows identifying a current owner are excluded.',
                           'Includes FREEAGENT and WAIVERS; this does not establish claim eligibility, waiver clearance, a free roster spot or unlocked players.']}


def _player_schedule(args, researcher):
    if _historical(researcher):
        return {'error': 'Current NFL affiliation cannot establish a historical player schedule.'}
    start, count = args.get('start_week', _week(researcher)), args.get('weeks', 4)
    if not _integer(start, 1, 18) or start < _period(researcher):
        return {'error': 'Choose this or a later NFL regular-season week through 18; historical affiliation is unavailable.'}
    data = _read_public(researcher, FANTASY + str(_year(researcher)), {'view': 'proTeamSchedules_wl'})
    schedules = {str(t['id']): t.get('proGamesByScoringPeriod', {})
                 for t in data.get('settings', {}).get('proTeams', [])[:40] if isinstance(t, dict) and 'id' in t}
    players = _cards(researcher, args['player_ids'])
    result = []
    for player in players:
        pro_id = str(getattr(player, 'pro_team_id', ''))
        schedule = schedules.get(pro_id, {})
        verified = {}
        for w, games in schedule.items():
            if not (str(w).isdigit() and 1 <= int(w) <= 18 and isinstance(games, list) and len(games) == 1):
                continue
            game = games[0]
            home, away, date = game.get('homeProTeamId'), game.get('awayProTeamId'), number(game.get('date'))
            if (pro_id in (str(home), str(away)) and home != away and home in PRO_TEAM_MAP and away in PRO_TEAM_MAP
                    and date is not None and 0 < date < 10_000_000_000_000):
                verified[int(w)] = date
        bye = next(iter(set(range(1, 19)) - set(verified))) if len(verified) == len(set(verified.values())) == 17 else None
        rows = []
        for week in range(start, min(19, start + count)):
            games = schedule.get(str(week), schedule.get(week, []))
            if not isinstance(games, list) or len(games) != 1:
                rows.append({'week': week, 'bye': week == bye if bye is not None else None, 'opponent': None})
                continue
            game = games[0]
            home, away = game.get('homeProTeamId'), game.get('awayProTeamId')
            if pro_id not in (str(home), str(away)):
                rows.append({'week': week, 'bye': None, 'opponent': None})
                continue
            opponent = away if str(home) == pro_id else home
            timestamp = number(game.get('date'))
            kickoff = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp is not None and 0 < timestamp < 10_000_000_000_000 else None
            rows.append({'week': week, 'opponent': PRO_TEAM_MAP.get(opponent),
                         'home_away': 'home' if str(home) == pro_id else 'away', 'kickoff': kickoff, 'bye': False})
        result.append({**_identity(player, researcher), 'schedule': rows, 'verified_bye_week': bye})
    return {'players': result, 'limitations': ['Uses the player current NFL team, not a historical trade/affiliation timeline.',
                                             'A bye is identified only from a complete 17-game regular-season schedule; absent games otherwise mean unknown. No opponent difficulty or lineup lock is inferred.']}


def _event(event, season, week):
    status = event.get('status', {}).get('type', {})
    competitions = event.get('competitions') or []
    competition = competitions[0] if competitions else {}
    if not status:
        status = competition.get('status', {}).get('type', {})
    state = status.get('state') if status.get('state') in ('pre', 'in', 'post') else 'unknown'
    teams = []
    for competitor in competition.get('competitors', [])[:2]:
        team = competitor.get('team', {})
        raw_score = competitor.get('score')
        if isinstance(raw_score, dict):
            raw_score = raw_score.get('value', raw_score.get('displayValue'))
        try:
            score = number(float(raw_score)) if state in ('in', 'post') else None
        except (ValueError, TypeError):
            score = None
        teams.append({'nfl_team': _text(team.get('abbreviation')), 'name': _text(team.get('displayName')),
                      'home_away': competitor.get('homeAway') if competitor.get('homeAway') in ('home', 'away') else None,
                      'nfl_score': score})
    return {'event_id': str(event.get('id', '')), 'season': season, 'week': week,
            'kickoff': _text(event.get('date', competition.get('date'))), 'state': state,
            'completed': status.get('completed') is True, 'status': _text(status.get('detail')),
            'teams': teams}


def _scoreboard(args, researcher):
    week = args.get('week', _week(researcher))
    if not _integer(week, 1, 18) or (_historical(researcher) and week > _week(researcher)):
        return {'error': 'Choose a regular-season week no later than this historical report.'}
    year = _year(researcher)
    data = _read_public(researcher, SCOREBOARD, {'dates': year, 'seasontype': 2, 'week': week, 'limit': 100})
    if data.get('season', {}).get('year') != year or data.get('season', {}).get('type') != 2 or data.get('week', {}).get('number') != week:
        return {'error': 'ESPN did not confirm the requested NFL season and week.'}
    games = []
    known = _cache(researcher).setdefault('nfl_events', {})
    for event in data.get('events', [])[:20]:
        if (event.get('season', {}).get('year') != year or event.get('season', {}).get('type') != 2
                or event.get('week', {}).get('number') != week or not re.fullmatch(r'\d{1,12}', str(event.get('id', '')))):
            continue
        game = _event(event, year, week)
        # Historical mode must not expose a later game's live/final information.
        known[game['event_id']] = {'week': week, 'season': year}
        games.append(game)
    return {'games': games, 'season': year, 'week': week,
            'limitations': ['NFL scores are not fantasy points. Pregame scores are omitted; event state is observed at fetch time.']}


def _summary(args, researcher):
    event_id = args['event_id']
    scope = _cache(researcher).get('nfl_events', {}).get(event_id)
    if not scope:
        return {'error': 'Use an event ID returned by get_nfl_scoreboard in this request.'}
    if _historical(researcher) and scope['week'] > _week(researcher):
        return {'error': 'A later NFL game is outside this historical report.'}
    data = _read_public(researcher, SUMMARY, {'event': event_id})
    header = data.get('header', {})
    season = header.get('season', {})
    if (str(header.get('id')) != event_id or season.get('year') != scope['season'] or season.get('type') != 2
            or header.get('week') != scope['week']):
        return {'error': 'ESPN game summary did not match the discovered event season and week.'}
    game = _event(header, scope['season'], scope['week'])
    team_stats, leaders, scoring = [], [], []
    if game['state'] in ('in', 'post'):
        for row in data.get('boxscore', {}).get('teams', [])[:2]:
            stats = [{'name': _text(s.get('label') or s.get('name')), 'value': _text(s.get('displayValue'), 50)}
                     for s in row.get('statistics', [])[:30] if isinstance(s, dict)]
            team_stats.append({'nfl_team': _text(row.get('team', {}).get('abbreviation')), 'statistics': stats})
        for group in data.get('leaders', [])[:2]:
            for category in group.get('leaders', [])[:6]:
                for leader in category.get('leaders', [])[:1]:
                    athlete = leader.get('athlete', {})
                    leaders.append({'nfl_team': _text(group.get('team', {}).get('abbreviation')),
                                    'category': _text(category.get('displayName') or category.get('name')),
                                    'player_id': str(athlete['id']) if re.fullmatch(r'\d{1,10}', str(athlete.get('id', ''))) else None,
                                    'name': _text(athlete.get('displayName')),
                                    'summary': _text(leader.get('displayValue'), 180)})
        for play in data.get('scoringPlays', [])[-6:]:
            scoring.append({'text': _text(play.get('text'), 240), 'quarter': play.get('period', {}).get('number'),
                            'clock': _text(play.get('clock', {}).get('displayValue'), 20),
                            'home_score': number(play.get('homeScore')), 'away_score': number(play.get('awayScore'))})
    return {'game': game, 'team_statistics': team_stats, 'leaders': leaders, 'recent_scoring_plays': scoring,
            'limitations': ['Selected NFL statistics and at most six recent scoring plays, not a full game account or fantasy point totals.',
                           'No articles, video transcripts, odds or current injury lists are included. NFL leader IDs require player discovery before fantasy-player tools.']}


def execute(name, args, researcher):
    """Strict arguments and a finite, request-local cache; no model URLs/headers."""
    if name not in NAMES or not isinstance(args, dict):
        return {'error': 'Unknown player tool or invalid arguments.'}
    schema = next(item['function']['parameters'] for item in TOOLS if item['function']['name'] == name)
    if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        return {'error': 'Use only the documented player-tool arguments.'}
    for key, value in args.items():
        spec = schema['properties'][key]
        if spec['type'] == 'integer' and not _integer(value, spec['minimum'], spec['maximum']):
            return {'error': 'Numeric argument is outside the supported range.'}
        if spec['type'] == 'number' and (number(value) is None or not spec['minimum'] <= value <= spec['maximum']):
            return {'error': 'Numeric argument is outside the supported range.'}
        if spec['type'] == 'string' and (not isinstance(value, str) or not value.strip()
                or ('enum' in spec and value not in spec['enum'])
                or ('minLength' in spec and not spec['minLength'] <= len(value.strip()) <= spec['maxLength'])
                or ('pattern' in spec and not re.fullmatch(spec['pattern'], value))):
            return {'error': 'Text argument is invalid.'}
        if key == 'player_ids' and (not isinstance(value, list) or not 1 <= len(value) <= 3
                or len(set(str(pid) for pid in value)) != len(value)
                or any(not isinstance(pid, str) or not re.fullmatch(r'-?\d{1,10}', pid) or pid not in researcher.allowed for pid in value)):
            return {'error': 'Use one to three distinct exact player IDs from the supplied roster or discovery tools.'}
    try:
        _check_deadline(researcher)
        cache = _cache(researcher)
        key = ('result', name, json.dumps(args, sort_keys=True))
        if key not in cache:
            if name == 'get_player_season_details':
                result = {'players': [_details(p, researcher, full=True) for p in _cards(researcher, args['player_ids'])],
                          'limitations': ['Missing stats are unknown, not zero. Current-week points may be incomplete.',
                                         'Historical mode omits later weeks, season aggregates, current ownership/status and non-archived projections.']}
            else:
                handler = {'search_players': _search, 'get_free_agent_pool': _free_agents,
                           'get_player_nfl_schedule': _player_schedule, 'get_nfl_scoreboard': _scoreboard,
                           'get_nfl_game_summary': _summary}[name]
                result = handler(args, researcher)
            if 'error' not in result:
                result.update(source='ESPN NFL' if name in ('get_nfl_scoreboard', 'get_nfl_game_summary') else 'ESPN Fantasy',
                              fetched_at=_stamp())
            cache[key] = result
        return cache[key]
    except Exception:
        return {'error': 'Player/NFL source unavailable; do not infer missing facts.'}
