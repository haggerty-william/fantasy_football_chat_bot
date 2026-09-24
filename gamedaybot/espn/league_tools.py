"""Read-only ESPN fantasy league evidence, scoped to one analysis request.

ESPN's fantasy API is undocumented; supported views and field meanings follow
the espn-api SDK, especially football/league.py, team.py and base_settings.py.
Never forward raw league responses: they can contain private member details.
"""
from datetime import datetime, timezone
import json
import statistics
import time

from espn_api.football.constant import POSITION_MAP, SETTINGS_SCORING_FORMAT_MAP

from gamedaybot.espn.analyst_evidence import finite
from gamedaybot.espn.decision_tools import TEAM, tool
from gamedaybot.espn.espn_read import read_league


PERIOD = {'matchup_period': {'type': 'integer', 'minimum': 1, 'maximum': 18,
                           'description': 'ESPN fantasy matchup period, which may span multiple scoring weeks.'}}
WEEK = {'week': {'type': 'integer', 'minimum': 1, 'maximum': 18,
                 'description': 'NFL scoring week, no later than the current/report week.'}}
TOOLS = [
    tool('get_fantasy_schedule', 'Get every remaining scheduled fantasy matchup, including beyond the next four weeks. Optional team filter. Future playoff pairings are provisional, never qualification guarantees.', TEAM),
    tool('get_week_matchups', 'Get one explicit fantasy matchup period: teams, scoring-week coverage, scores and completion status. Can inspect future matchups without treating placeholder zero scores as results.', {**PERIOD, **TEAM}, ['matchup_period']),
    tool('get_week_box_scores', 'Get both lineups in one team\'s current or past scoring-week matchup, player actuals/projections and scoring contributions. Future lineups are unavailable.', {**WEEK, **TEAM}, ['week', 'team_id']),
    tool('get_league_rules', 'Get verified scoring categories, position overrides, lineup slots, waiver/FAAB, trade deadline/veto, keeper and playoff rules. Read-only; no rule changes.'),
    tool('get_team_profile', 'Get a team\'s current roster, division, record splits, waiver priority, FAAB and acquisition/drop/trade counts. Does not expose private owner details.', TEAM, ['team_id']),
    tool('get_scoring_splits', 'Compare completed matchup scoring averages, medians, spread, last-three form and opponent scoring. Periods may span multiple weeks; sample coverage is explicit.', TEAM),
    tool('get_draft_board', 'Read draft picks, original drafting team, keeper flags and auction costs. Paginated by offset; optional fantasy team filter. Current rosters do not replace original draft ownership.',
         {**TEAM, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2000},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 60}}),
    tool('get_head_to_head', 'Read completed and upcoming meetings between two teams in this season, with aggregate head-to-head scores and official results. No invented multi-season rivalry history.',
         {**TEAM, 'opponent_team_id': {'type': 'string', 'description': 'Another exact key from research_context.teams.'}}, ['team_id', 'opponent_team_id']),
]
NAMES = {item['function']['name'] for item in TOOLS}
SOURCE = 'https://fantasy.espn.com/football/league'
BASE_VIEWS = ('mSettings', 'mTeam', 'mMatchupScore')


def check_time(researcher):
    if time.monotonic() >= researcher.deadline - 20:
        raise TimeoutError('Research budget exhausted')


def read_views(researcher, views, week=None, matchup_periods=None):
    """Fixed-host, bounded authenticated read; cache expires with this request.

    This bypasses the SDK request logger and its unbounded HTTP calls. Views
    are chosen by application code, never forwarded from model arguments.
    """
    check_time(researcher)
    if week is not None and (type(week) is not int or not 1 <= week <= 18):
        raise ValueError('Invalid scoring week')
    if matchup_periods is not None and (not isinstance(matchup_periods, (list, tuple))
            or not matchup_periods or len(matchup_periods) > 18
            or any(type(p) is not int or not 1 <= p <= 18 for p in matchup_periods)):
        raise ValueError('Invalid matchup periods')
    key = (tuple(sorted(views)), week, tuple(matchup_periods or []))
    cache = researcher.__dict__.setdefault('league_view_cache', {})
    if key in cache:
        return cache[key]
    params = {}
    if week is not None:
        params['scoringPeriodId'] = week
    filters = {'schedule': {'filterMatchupPeriodIds': {'value': list(matchup_periods)}}} if matchup_periods else None
    data = read_league(researcher.league, views, params=params, filters=filters,
                       deadline=researcher.deadline - 20, timeout=15, max_bytes=4_000_000)
    if not isinstance(data, dict):
        raise ValueError('ESPN response schema unavailable')
    cache[key] = data
    return data


def _num(value):
    return round(value, 3) if finite(value) else None


def _text(value, limit=150):
    return value[:limit] if isinstance(value, str) else None


def _identity(researcher, team_id):
    team = next((t for t in researcher.league.teams if str(t.team_id) == str(team_id)), None)
    return {'team_id': str(team.team_id), 'team': team.team_name} if team is not None else None


def _periods(researcher, data):
    raw = data.get('settings', {}).get('scheduleSettings', {}).get('matchupPeriods')
    if not isinstance(raw, dict):
        raw = getattr(researcher.league.settings, 'matchup_periods', {})
    return {int(k): v for k, v in raw.items() if str(k).isdigit() and 1 <= int(k) <= 18
            and isinstance(v, list) and v and all(type(w) is int and 1 <= w <= 18 for w in v)}


def _period_state(researcher, weeks):
    if not weeks:
        return 'unknown'
    current = getattr(researcher.league, 'scoringPeriodId', researcher.week)
    if max(weeks) < current and max(weeks) <= researcher.week:
        return 'completed'
    if min(weeks) > researcher.week or min(weeks) > current:
        return 'future'
    return 'in_progress'


def _matches(researcher, data):
    periods = _periods(researcher, data)
    rows = []
    source_rows = data.get('schedule')
    if not isinstance(source_rows, list):
        raise ValueError('Fantasy schedule unavailable')
    if len(source_rows) > 512:
        raise ValueError('Fantasy schedule exceeds supported league size')
    for raw in source_rows:
        if not isinstance(raw, dict):
            continue
        period = raw.get('matchupPeriodId')
        if type(period) is not int or period not in periods:
            continue
        weeks = periods[period]
        state = _period_state(researcher, weeks)
        # Historical reports must not receive later scores or now-resolved
        # playoff opponents that were unknown at that report's cutoff.
        if researcher.context.get('historical') and max(weeks) > researcher.week:
            continue
        sides = []
        for side in ('home', 'away'):
            value = raw.get(side, {})
            identity = _identity(researcher, value.get('teamId'))
            if identity:
                sides.append({**identity, 'side': side,
                              'score': _num(value.get('totalPoints')) if state != 'future' else None})
        if not sides:
            continue
        playoff = raw.get('playoffTierType', 'NONE')
        winner = raw.get('winner') if state == 'completed' else None
        rows.append({'matchup_period': period, 'scoring_weeks': weeks, 'state': state,
                     'playoff_tier': _text(playoff), 'pairing_provisional': state == 'future' and playoff != 'NONE',
                     'teams': sides, 'bye': len(sides) == 1,
                     'winner_side': winner if winner in ('HOME', 'AWAY', 'TIE') else None})
    return sorted(rows, key=lambda row: row['matchup_period'])


def _for_team(rows, team_id):
    return [r for r in rows if team_id is None or any(t['team_id'] == team_id for t in r['teams'])]


def fantasy_schedule(researcher, team_id=None):
    data = read_views(researcher, BASE_VIEWS)
    rows = _for_team(_matches(researcher, data), team_id)
    rows = [r for r in rows if r['state'] != 'completed']
    periods = {}
    for row in rows:
        key = row['matchup_period']
        group = periods.setdefault(key, {'matchup_period': key, 'scoring_weeks': row['scoring_weeks'],
                                        'state': row['state'], 'team_ids': [], 'pairings': []})
        sides = {side['side']: side for side in row['teams']}
        home, away = sides.get('home', {}), sides.get('away', {})
        group['pairings'].append([home.get('team_id'), away.get('team_id'), home.get('score'), away.get('score'),
                                  row['playoff_tier'], row['pairing_provisional'], row['bye']])
        group['team_ids'] = sorted(set(group['team_ids']) | {side['team_id'] for side in row['teams']})
    return {'from_scoring_week': researcher.week, 'matchups': list(periods.values()),
            'pairing_columns': ['home_team_id', 'away_team_id', 'home_score', 'away_score',
                                'playoff_tier', 'pairing_provisional', 'bye'],
            'returned_matchup_count': len(rows), 'returned_period_count': len(periods), 'omitted_matchups': 0,
            'limitations': 'All remaining ESPN pairings, grouped by period; pairing_columns identifies each compact row. team_ids lists participants in the period, NOT who faces whom: use the pairings. Current totals are retained; future scores are unknown, not zero. No matchup here has a confirmed winner. Missing weeks/opponents are unknown, not confirmed byes. Future playoff pairings can change; no projections or qualification guarantees.'}


def week_matchups(researcher, period, team_id=None):
    data = read_views(researcher, BASE_VIEWS)
    if period not in _periods(researcher, data):
        return {'error': 'Unknown fantasy matchup period; use get_league_rules for scoring-week coverage.'}
    rows = [r for r in _for_team(_matches(researcher, data), team_id) if r['matchup_period'] == period]
    return {'matchup_period': period, 'matchups': rows,
            'limitations': 'Scores are totals for the full fantasy matchup period, which may span several NFL scoring weeks. In-progress totals can change. Future placeholder scores are omitted. Empty means unavailable at this report cutoff.'}


def _player(entry, week, year):
    pool = entry.get('playerPoolEntry', {})
    p = pool.get('player', {})
    pid = p.get('id', entry.get('playerId'))
    if type(pid) is not int or not isinstance(p.get('fullName'), str):
        return None
    stats = [s for s in p.get('stats', []) if isinstance(s, dict)
             and s.get('seasonId') == year and s.get('scoringPeriodId') == week
             and s.get('statSplitTypeId') != 2]
    actual = next((s for s in stats if s.get('statSourceId') == 0), {})
    projected = next((s for s in stats if s.get('statSourceId') == 1), {})
    contributions = actual.get('appliedStats', {})
    nonzero = [(str(k), v) for k, v in contributions.items() if finite(v) and v != 0]
    nonzero.sort(key=lambda item: abs(item[1]), reverse=True)
    stat_points = [{'stat_id': k, 'points': _num(v)} for k, v in nonzero[:6]]
    return {'id': str(pid), 'name': _text(p['fullName']),
            'position': POSITION_MAP.get(p.get('defaultPositionId'), 'Unknown'),
            'lineup_slot': POSITION_MAP.get(entry.get('lineupSlotId'), 'Unknown'),
            'points': _num(actual.get('appliedTotal')), 'projected_points': _num(projected.get('appliedTotal')),
            'scoring_contributions': stat_points,
            'omitted_nonzero_contributions': max(0, len(nonzero) - len(stat_points))}


def week_box_scores(researcher, week, team_id):
    current = min(getattr(researcher.league, 'current_week', researcher.week), researcher.week)
    if week > current:
        return {'error': 'Future box scores are unavailable; use get_week_matchups for future pairings.'}
    periods = {p for p, weeks in _periods(researcher, {}).items() if week in weeks}
    if not periods:
        return {'error': 'Scoring week does not map to a known fantasy matchup period.'}
    data = read_views(researcher, ('mSettings', 'mMatchupScore', 'mScoreboard'), week, sorted(periods))
    selected = [m for m in data.get('schedule', []) if m.get('matchupPeriodId') in periods
                and any(str(m.get(side, {}).get('teamId')) == team_id for side in ('home', 'away'))]
    if len(selected) != 1:
        return {'error': 'Cannot uniquely identify the requested team matchup.'}
    teams = []
    for side in ('home', 'away'):
        raw = selected[0].get(side, {})
        identity = _identity(researcher, raw.get('teamId'))
        if identity is None:
            continue
        entries = raw.get('rosterForCurrentScoringPeriod', {}).get('entries')
        if not isinstance(entries, list) or len(entries) > 60:
            return {'error': 'Requested historical lineup unavailable or exceeds limits.'}
        players = [_player(entry, week, researcher.league.year) for entry in entries]
        teams.append({**identity, 'side': side, 'players': [p for p in players if p],
                      'omitted_player_entries': sum(p is None for p in players)})
    result = {'week': week, 'matchup_period': selected[0]['matchupPeriodId'], 'teams': teams,
            'state': 'completed' if week < getattr(researcher.league, 'scoringPeriodId', researcher.week) else 'in_progress',
            'returned_player_count': sum(len(team['players']) for team in teams),
            'omitted_player_count': sum(team['omitted_player_entries'] for team in teams),
            'scoring_contribution_policy': 'Zero categories omitted. At most six largest absolute nonzero contributions per player, further reduced if needed to fit the evidence budget. omitted_nonzero_contributions counts omitted categories; player point totals and projections are never reconstructed from this partial breakdown.',
            'limitations': 'Historical scoring-week roster snapshots from ESPN, with actual and projected points kept separate. Missing statistics remain unknown, not zero. Projections are ESPN values at fetch time, not guaranteed original pregame forecasts. Scoring contribution IDs map to get_league_rules; no injury status from today is attached to old lineups.'}
    # Large real ESPN lineups contain many zero categories. Keep every player's
    # ID, actual total and projection, spending only remaining space on optional
    # category breakdowns. Leave room for dispatcher source/time metadata.
    all_players = [player for team in teams for player in team['players']]
    while len(json.dumps(result, ensure_ascii=False)) > 13200:
        detailed = [p for p in all_players if p['scoring_contributions']]
        if not detailed:
            return {'error': 'The complete requested lineups exceed the evidence budget; no player totals were silently omitted.'}
        player = max(detailed, key=lambda p: len(p['scoring_contributions']))
        player['scoring_contributions'].pop()
        player['omitted_nonzero_contributions'] += 1
    result['omitted_scoring_contribution_count'] = sum(p['omitted_nonzero_contributions'] for p in all_players)
    return result


def _fields(raw, names):
    return {name: raw[name] if isinstance(raw[name], bool) else _text(raw[name]) if isinstance(raw[name], str) else _num(raw[name])
            for name in names if name in raw and isinstance(raw[name], (str, bool, int, float))}


def league_rules(researcher):
    data = read_views(researcher, BASE_VIEWS)
    settings = data.get('settings', {})
    if not isinstance(settings, dict) or not settings:
        return {'error': 'Verified league rules unavailable.'}
    scoring = []
    for item in settings.get('scoringSettings', {}).get('scoringItems', [])[:150]:
        stat_id = item.get('statId')
        overrides = item.get('pointsOverrides', {})
        scoring.append({'stat_id': stat_id, 'label': SETTINGS_SCORING_FORMAT_MAP.get(stat_id, {}).get('label', 'Unknown ESPN scoring category'),
                        'points': _num(item.get('points')),
                        'position_overrides': {str(k): _num(v) for k, v in list(overrides.items())[:30] if finite(v)}})
    slots = settings.get('rosterSettings', {}).get('lineupSlotCounts', {})
    result = {'matchup_periods': _periods(researcher, data), 'scoring': scoring,
              'lineup_slots': {POSITION_MAP.get(int(k), str(k)): v for k, v in slots.items() if str(k).isdigit() and type(v) is int and v > 0},
              'divisions': [{'id': row.get('id'), 'name': _text(row.get('name'))} for row in settings.get('scheduleSettings', {}).get('divisions', [])[:32]]}
    groups = {
        'scoring_rules': ('scoringSettings', ('scoringType', 'matchupTieRule', 'playoffMatchupTieRule', 'scoringEnhancementType')),
        'playoffs': ('scheduleSettings', ('matchupPeriodCount', 'playoffTeamCount', 'playoffMatchupPeriodLength', 'playoffSeedingRule', 'playoffSeedingRuleBy', 'playoffReseed')),
        'waivers': ('acquisitionSettings', ('isUsingAcquisitionBudget', 'acquisitionBudget', 'acquisitionLimit', 'matchupAcquisitionLimit', 'acquisitionType', 'waiverHours', 'waiverOrderReset', 'isUsingUndroppableList')),
        'trades': ('tradeSettings', ('deadlineDate', 'vetoVotesRequired', 'reviewPeriod', 'maxTrades')),
        'draft': ('draftSettings', ('type', 'date', 'keeperCount', 'auctionBudget', 'isTradingEnabled', 'isDraftOrderManuallySet')),
        'roster_rules': ('rosterSettings', ('isBenchUnlimited', 'isUsingUndroppableList', 'lineupLocktimeType', 'rosterLocktimeType')),
    }
    for name, (source, fields) in groups.items():
        result[name] = _fields(settings.get(source, {}), fields)
    result['limitations'] = 'Current ESPN rules snapshot, not a historical rules archive. Omitted fields are unavailable. Dates are ESPN Unix epoch milliseconds. Scoring overrides retain ESPN position IDs; no commissioner settings are changed.'
    return result


def team_profile(researcher, team_id):
    data = read_views(researcher, BASE_VIEWS)
    raw = next((r for r in data.get('teams', []) if str(r.get('id')) == team_id), None)
    if raw is None:
        return {'error': 'Team details unavailable.'}
    team = next(t for t in researcher.league.teams if str(t.team_id) == team_id)
    settings = data.get('settings', {})
    waivers = settings.get('acquisitionSettings', {})
    counters = _fields(raw.get('transactionCounter', {}), ('acquisitions', 'acquisitionBudgetSpent', 'drops', 'trades', 'moveToIR'))
    faab, budget, spent = waivers.get('isUsingAcquisitionBudget'), _num(waivers.get('acquisitionBudget')), counters.get('acquisitionBudgetSpent')
    roster = []
    for p in getattr(team, 'roster', [])[:60]:
        if not hasattr(p, 'playerId'):
            continue
        roster.append({'id': str(p.playerId), 'name': _text(getattr(p, 'name', None)),
                       'position': _text(getattr(p, 'position', None)), 'nfl_team': _text(getattr(p, 'proTeam', None)),
                       'lineup_slot': _text(getattr(p, 'lineupSlot', getattr(p, 'slot_position', None))),
                       'current_status': _text(getattr(p, 'injuryStatus', None)),
                       'acquisition_type': _text(getattr(p, 'acquisitionType', None))})
    records = {kind: _fields(value, ('wins', 'losses', 'ties', 'percentage', 'pointsFor', 'pointsAgainst', 'streakLength', 'streakType'))
               for kind, value in raw.get('record', {}).items() if kind in ('overall', 'division', 'home', 'away') and isinstance(value, dict)}
    division = next((d for d in settings.get('scheduleSettings', {}).get('divisions', []) if d.get('id') == raw.get('divisionId')), {})
    return {**_identity(researcher, team_id), 'division': {'id': raw.get('divisionId'), 'name': _text(division.get('name'))},
            'records': records, 'playoff_seed': _num(raw.get('playoffSeed')), 'waiver_rank': _num(raw.get('waiverRank')),
            'transaction_counts': counters, 'faab_enabled': faab if type(faab) is bool else None,
            'faab_initial_budget': budget if faab is True else None,
            'faab_remaining_estimate': round(budget - spent, 2) if faab is True and budget is not None and spent is not None else None,
            'roster': roster, 'omitted_roster_players': max(0, len(getattr(team, 'roster', [])) - 60),
            'limitations': 'Current league snapshot and roster. FAAB remaining is initial budget minus recorded spending; commissioner adjustments may differ. Transaction counts do not imply move quality or pending trades. ACTIVE is not proof of full health; missing values are unknown.'}


def scoring_splits(researcher, team_id=None):
    data = read_views(researcher, BASE_VIEWS)
    matches = [r for r in _matches(researcher, data) if r['state'] == 'completed' and not r['bye']]
    rows = []
    for team in researcher.league.teams[:32]:
        tid = str(team.team_id)
        if team_id is not None and tid != team_id:
            continue
        games, omitted = [], []
        for m in _for_team(matches, tid):
            ours = next(t for t in m['teams'] if t['team_id'] == tid)
            other = next(t for t in m['teams'] if t['team_id'] != tid)
            if ours['score'] is None or other['score'] is None:
                omitted.append(m['matchup_period'])
                continue
            games.append({'matchup_period': m['matchup_period'], 'scoring_weeks': m['scoring_weeks'],
                          'points': ours['score'], 'opponent_team_id': other['team_id'], 'opponent_points': other['score'],
                          'margin': round(ours['score'] - other['score'], 2)})
        values = [g['points'] for g in games]
        rows.append({**_identity(researcher, tid), 'covered_matchups': len(games), 'games': games, 'omitted_matchup_periods': omitted,
                     'mean_points_per_matchup': round(statistics.mean(values), 2) if values else None,
                     'median_points_per_matchup': round(statistics.median(values), 2) if values else None,
                     'points_population_stddev': round(statistics.pstdev(values), 2) if len(values) > 1 else None,
                     'highest_matchup_score': max(values) if values else None, 'lowest_matchup_score': min(values) if values else None,
                     'last_three_mean': round(statistics.mean(values[-3:]), 2) if values else None,
                     'last_three_sample': min(3, len(values)),
                     'mean_opponent_points': round(statistics.mean(g['opponent_points'] for g in games), 2) if games else None})
    return {'through_scoring_week': min(researcher.week, getattr(researcher.league, 'scoringPeriodId', researcher.week) - 1),
            'teams': rows, 'limitations': 'Completed non-bye fantasy matchup periods only, not necessarily single weeks. Mixed-length periods are not directly comparable. Missing scores omit that meeting; empty samples are unknown. Variability and recent form describe observed scores, not sustainable team strength or causal management skill.'}


def draft_board(researcher, team_id=None, offset=0, limit=40):
    data = read_views(researcher, ('mDraftDetail',))
    detail = data.get('draftDetail')
    if not isinstance(detail, dict):
        return {'error': 'Draft details unavailable.'}
    picks = detail.get('picks', [])
    if not isinstance(picks, list) or len(picks) > 2000:
        return {'error': 'Draft board unavailable or exceeds supported size.'}
    selected = [p for p in picks if isinstance(p, dict) and (team_id is None or str(p.get('teamId')) == team_id)]
    # Preserve API draft order, rather than falsely inventing an overall pick
    # number for auction nominations or keeper arrangements.
    teams = {}
    omitted = 0
    for p in selected[offset:offset + limit]:
        identity = _identity(researcher, p.get('teamId'))
        if identity is None or type(p.get('playerId')) is not int:
            omitted += 1
            continue
        pid = p['playerId']
        row = {'player_id': str(pid), 'player': _text(getattr(researcher.league, 'player_map', {}).get(pid)),
               'round': _num(p.get('roundId')), 'pick_in_round': _num(p.get('roundPickNumber')),
               'bid_amount': _num(p.get('bidAmount')), 'keeper': p.get('keeper') if type(p.get('keeper')) is bool else None}
        teams.setdefault(identity['team_id'], {**identity, 'picks': []})['picks'].append(row)
    end = offset + min(limit, max(0, len(selected) - offset))
    return {'teams': list(teams.values()), 'drafted': detail.get('drafted') if type(detail.get('drafted')) is bool else None,
            'offset': offset, 'total_matching_picks': len(selected), 'next_offset': end if end < len(selected) else None,
            'omitted_page_entries': omitted,
            'limitations': 'Original drafting owner, not current roster owner. ESPN order retained within this page; no inferred ADP or keeper value. Missing player names/costs/keeper flags are unknown. Draft positions alone do not establish management quality.'}


def head_to_head(researcher, team_id, opponent_id):
    data = read_views(researcher, BASE_VIEWS)
    rows = [r for r in _for_team(_matches(researcher, data), team_id)
            if any(t['team_id'] == opponent_id for t in r['teams'])]
    completed = [r for r in rows if r['state'] == 'completed' and r['winner_side'] is not None]
    record = {'wins': 0, 'losses': 0, 'ties': 0}
    for m in completed:
        ours = next(t for t in m['teams'] if t['team_id'] == team_id)
        key = 'ties' if m['winner_side'] == 'TIE' else 'wins' if ours['side'].upper() == m['winner_side'] else 'losses'
        record[key] += 1
    return {**_identity(researcher, team_id), 'opponent_team_id': opponent_id,
            'season': researcher.league.year, 'completed_record': record, 'matchups': rows,
            'completed_meetings_without_official_result': sum(r['state'] == 'completed' and r['winner_side'] is None for r in rows),
            'limitations': 'This league season only. Record uses ESPN official winner flags for completed periods, including applicable tiebreaks; scores alone do not override it. Future playoff pairings are provisional. No historic manager identity or long-running rivalry is inferred.'}


def build_schedule_awareness(league, week=None):
    """Small default schedule context using the already-fetched SDK snapshot.

    SDK schedule entries have no period IDs; only use its indexing convention
    for consecutive one-week periods, and verify opponent reciprocity. The
    full tool reads explicit API period IDs when these checks cannot hold.
    """
    reference = week if week is not None else getattr(league, 'current_week', None)
    settings = getattr(league, 'settings', None)
    periods = getattr(settings, 'matchup_periods', {})
    note = ('Current league schedule snapshot; at most three current/upcoming periods. No scores or projections inferred. '
            'Future playoff pairings are provisional. Use get_fantasy_schedule for every remaining pairing or get_week_matchups for explicit periods.')
    if (type(reference) is not int or not 1 <= reference <= 18 or not isinstance(periods, dict)
            or not periods or any(not str(k).isdigit() or v != [int(k)] for k, v in periods.items())
            or sorted(int(k) for k in periods) != list(range(1, len(periods) + 1))):
        return {'matchups': [], 'status': 'Default schedule unavailable for unverified or multiweek period indexing; use get_fantasy_schedule.', 'limitations': note}
    teams = {str(t.team_id): t for t in getattr(league, 'teams', [])[:32]}
    rows, missing = [], []
    regular = getattr(settings, 'reg_season_count', None)
    for period in range(reference, min(reference + 3, len(periods) + 1, 19)):
        seen = set()
        for tid, team in teams.items():
            schedule = getattr(team, 'schedule', [])
            other = schedule[period - 1] if period <= len(schedule) else None
            other_id = str(getattr(other, 'team_id', ''))
            if other_id not in teams:
                missing.append({'team_id': tid, 'matchup_period': period})
                continue
            pair = tuple(sorted({tid, other_id}))
            if pair in seen:
                continue
            reciprocal = getattr(teams[other_id], 'schedule', [])
            if (len(pair) == 2 and (period > len(reciprocal)
                    or str(getattr(reciprocal[period - 1], 'team_id', '')) != tid)):
                missing.append({'team_id': tid, 'matchup_period': period})
                continue
            seen.add(pair)
            rows.append({'matchup_period': period, 'scoring_weeks': [period], 'team_ids': list(pair),
                         'bye': len(pair) == 1,
                         'pairing_provisional': type(regular) is not int or period > regular})
    return {'from_scoring_week': reference, 'matchups': rows, 'unavailable': missing,
            'scope': 'Current season only, loaded ESPN snapshot', 'limitations': note}


def execute(name, args, researcher):
    schemas = {item['function']['name']: item['function']['parameters'] for item in TOOLS}
    if name not in schemas or not isinstance(args, dict):
        return {'error': 'Invalid league tool arguments.'}
    schema = schemas[name]
    if not set(schema['required']) <= set(args) or not set(args) <= set(schema['properties']):
        return {'error': 'Invalid league tool arguments.'}
    for key, value in args.items():
        spec = schema['properties'][key]
        if spec['type'] == 'string':
            if not isinstance(value, str) or _identity(researcher, value) is None:
                return {'error': 'Use an exact fantasy team ID.'}
        elif type(value) is not int or not spec['minimum'] <= value <= spec['maximum']:
            return {'error': 'Use a whole number in the tool\'s stated bounds.'}
    if name == 'get_head_to_head' and args['team_id'] == args['opponent_team_id']:
        return {'error': 'Choose two different fantasy teams.'}
    if researcher.context.get('historical') and name in ('get_team_profile', 'get_fantasy_schedule', 'get_league_rules'):
        return {'error': 'Current roster, rules and future schedule excluded from historical reports.'}
    try:
        check_time(researcher)
        if name == 'get_fantasy_schedule': result = fantasy_schedule(researcher, args.get('team_id'))
        elif name == 'get_week_matchups': result = week_matchups(researcher, args['matchup_period'], args.get('team_id'))
        elif name == 'get_week_box_scores': result = week_box_scores(researcher, args['week'], args['team_id'])
        elif name == 'get_league_rules': result = league_rules(researcher)
        elif name == 'get_team_profile': result = team_profile(researcher, args['team_id'])
        elif name == 'get_scoring_splits': result = scoring_splits(researcher, args.get('team_id'))
        elif name == 'get_draft_board': result = draft_board(researcher, args.get('team_id'), args.get('offset', 0), args.get('limit', 40))
        else: result = head_to_head(researcher, args['team_id'], args['opponent_team_id'])
        if 'error' not in result:
            result.update(source=SOURCE, fetched_at=datetime.now(timezone.utc).isoformat())
        return result
    except Exception:
        # Never expose URLs with private filters, cookies, member data or a raw
        # HTTP exception in model evidence or logs.
        return {'error': 'ESPN league evidence unavailable; do not infer missing facts.'}
