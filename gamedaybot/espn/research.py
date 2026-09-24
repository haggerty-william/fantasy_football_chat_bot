"""Bounded ESPN evidence for local inference using fixed, trusted sources."""

from datetime import datetime, timezone, timedelta
import json
import math
import re
from threading import Lock
import time
import unicodedata
from urllib.parse import urlsplit

import requests

NEWS_URL = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/news'
_cache = None
_cache_lock = Lock()


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value)).casefold()
                   if not unicodedata.combining(c))


def number(value):
    return round(value, 2) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def news_feed():
    """Cache successful feeds for ten minutes; never silently serve stale news."""
    global _cache
    with _cache_lock:
        if _cache and time.monotonic() - _cache[0] < 600:
            return _cache[1], _cache[2]
        started = time.monotonic()
        with requests.get(NEWS_URL, params={'limit': 100}, timeout=(3, 8),
                          allow_redirects=False, stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > 1_000_000 or time.monotonic() - started > 12:
                    raise ValueError('News response limit')
                chunks.append(chunk)
            articles = json.loads(b''.join(chunks)).get('articles')
            if not isinstance(articles, list):
                raise ValueError('Invalid news response')
        fetched = datetime.now(timezone.utc).isoformat()
        _cache = (time.monotonic(), articles[:100], fetched)
        return _cache[1], fetched


def relevant_news(names, now):
    articles, fetched = news_feed()
    selected, seen = [], set()
    for article in articles:
        try:
            published = datetime.fromisoformat(article['published'].replace('Z', '+00:00'))
            if not now - timedelta(days=3) <= published <= now:
                continue
            headline = str(article['headline'])[:250]
            summary = re.sub('<[^>]+>', '', str(article.get('description', '')))[:280]
            body = normalized(headline + ' ' + summary)
            matched = [name for name in names if re.search(r'(?<!\w)' + re.escape(normalized(name)) + r'(?!\w)', body)]
            if not matched:
                continue
            url = article['links']['web']['href']
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or
                    not (parsed.hostname == 'espn.com' or parsed.hostname.endswith('.espn.com')) or
                    parsed.username or parsed.password or url in seen):
                continue
            seen.add(url)
            selected.append({'headline': headline, 'summary': summary, 'published_at': published.isoformat(),
                             'url': url, 'players': matched})
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    selected.sort(key=lambda article: article['published_at'], reverse=True)
    return selected[:6], fetched


def fantasy_team_context(league):
    """Current ESPN manager names only; never pass account IDs or contact data."""
    teams = []
    def clean(value):
        return ' '.join(value.split())[:80] if isinstance(value, str) else ''
    for team in getattr(league, 'teams', []):
        if not hasattr(team, 'team_id'):
            continue
        managers = []
        for owner in getattr(team, 'owners', []) or []:
            if not isinstance(owner, dict):
                continue
            first, last = clean(owner.get('firstName')), clean(owner.get('lastName'))
            name = ' '.join(part for part in (first, last) if part)
            name = name or clean(owner.get('displayName'))
            if name and name not in managers:
                managers.append(name)
        teams.append({'id': str(team.team_id), 'name': team.team_name, 'managers': managers})
    return teams


def _prune_context(context, budget, report_type):
    """Keep explanations and identities before optional detail consumes space.

    Standings are explained by completed scoring, opponent strength and luck,
    not by a full NFL scoreboard or the entire scoring-rules catalog. Preserve
    those per-team comparisons and their metric definitions together.
    """
    context['context_char_limit'] = budget
    context['omitted_roster_players'] = 0
    size = lambda: len(json.dumps(context))

    def omitted(field, count=1):
        counts = context.setdefault('context_omissions', {})
        counts[field] = counts.get(field, 0) + count
        context['context_omission_note'] = ('Omitted data is unknown, not zero or proof of none. '
                                            'Use the research tools for additional detail.')

    history = context.get('league_history', {})
    forecasts = history.get('previous_forecasts', [])
    while forecasts and size() > budget:
        forecasts.pop()
        omitted('previous_forecasts')

    if report_type in ('get_standings', 'get_power_rankings', 'get_playoffs') and size() > budget:
        games = context.get('nfl_games', {})
        if games:
            omitted('nfl_games', len(games))
            context.pop('nfl_games')
            context['game_state_status'] = 'NFL scoreboard detail omitted for standings context; use player status or NFL scoreboard tools.'

    rules = context.get('league_rules', {})
    scoring = rules.get('scoring', [])
    while scoring and size() > budget:
        scoring.pop()
        omitted('scoring_rules')
        rules['scoring_status'] = 'Scoring categories omitted for context; use get_league_rules before assuming a complete scoring system.'

    while context['players'] and size() > budget:
        context['players'].pop()
        context['omitted_player_count'] += 1
    for field in ('news', 'highlights', 'current_roster_depth'):
        while context.get(field) and size() > budget:
            context[field].pop()
            omitted(field)

    # Keep every team's roster shell and a count of missing entries even when
    # the detailed roster must be fetched later. Avoid starving just one team.
    while any(row['players'] for row in context['rosters']) and size() > budget:
        largest = max(context['rosters'], key=lambda row: len(row['players']))
        largest['players'].pop()
        largest['omitted_players'] = largest.get('omitted_players', 0) + 1
        context['omitted_roster_players'] += 1

    # Secondary history can be recovered by tools. Never replace the whole
    # history object and silently discard every team's explanatory evidence.
    for field in ('meetings', 'recent_trades'):
        while history.get(field) and size() > budget:
            history[field].pop(0)
            omitted('history_' + field)
    if size() > budget and context.get('nfl_games'):
        omitted('nfl_games', len(context['nfl_games']))
        context.pop('nfl_games')
        context['game_state_status'] = 'NFL game-state detail omitted; lineup availability is unknown without a status lookup.'

    # In unusual leagues, keep one recent game and the core season comparison
    # metrics per team; discard redundant component totals before explanations.
    team_history = history.get('teams', [])
    while size() > budget:
        rows = [row['performance']['recent_games'] for row in team_history
                if isinstance(row.get('performance'), dict)
                and len(row['performance'].get('recent_games', [])) > 1]
        if not rows:
            break
        max(rows, key=len).pop(0)
        omitted('older_performance_games')
    if size() > budget:
        redundant = ('all_play_wins', 'all_play_ties', 'all_play_losses', 'points_for', 'points_against')
        for row in team_history:
            performance = row.get('performance', {})
            for field in redundant:
                if field in performance:
                    performance.pop(field)
                    omitted('performance_component_totals')

    # Keep trade states visible even if a busy league's extra player legs need
    # a follow-up tool. The collection count remains a visible-scope count.
    awareness = context.get('trade_awareness', {})
    for state in ('recent_completed', 'pending'):
        collection = awareness.get(state, {})
        trades = collection.get('trades', [])
        while len(trades) > 1 and size() > budget:
            trades.pop()
            collection['truncated'] = True
            omitted(state + '_trades')
        for trade in trades:
            while trade.get('items') and size() > budget:
                trade['items'].pop()
                trade['items_complete'] = False
                trade['omitted_items'] = trade.get('omitted_items', 0) + 1
                omitted('trade_items')

    # A hard fallback still retains canonical identities, all per-team core
    # performance, provenance/cutoffs, and compact awareness. Optional arrays
    # never displace those facts just because a league has unusual settings.
    for field in ('matchups',):
        while context.get(field) and size() > budget:
            context[field].pop()
            omitted(field)
    if size() > budget:
        # Exact trade directions and team explanations are authoritative. Let
        # the caller use its explicit unavailable-context fallback rather than
        # silently erase those facts or exceed the advertised model budget.
        raise ValueError('Core report evidence exceeds the supported context budget.')
    return context


def build_context(league, report, report_type, week=None, box_scores=None, trade_actions=None, requested_player_ids=None):
    from gamedaybot.espn import analyst_evidence as evidence
    from gamedaybot.espn.nfl_usage import usage_context
    from gamedaybot.espn.community import nfl_games
    import os
    now = datetime.now(timezone.utc)
    period = getattr(league, 'scoringPeriodId', None)
    week = week if week is not None else period
    historical = isinstance(week, int) and isinstance(period, int) and week < period
    context = {'source': 'ESPN Fantasy', 'fetched_at': now.isoformat(), 'week': week,
               'historical': historical, 'status_updated_at': None, 'players': [], 'news': [],
               'limitations': ['Missing data is unknown, not zero. ACTIVE is not proof of full health.',
                              'News briefs are attributed reports, not confirmed availability.']}
    groups = []
    if box_scores is not None:
        for box in box_scores:
            for side in ('home', 'away'):
                team = getattr(box, side + '_team', None)
                if team is not None:
                    groups.append((team, getattr(box, side + '_lineup', [])))
    else:
        groups = [(team, getattr(team, 'roster', [])) for team in getattr(league, 'teams', [])]
    if report_type == 'get_rivalry':
        focused = [(t,r) for t,r in groups if normalized(t.team_name) in normalized(report)]
        if focused: groups = focused
    involved = {getattr(t, 'team_id', None) for t,_,_,*_ in trade_actions or []}
    if involved:
        groups = [(t,r) for t,r in groups if getattr(t,'team_id',None) in involved]
    elif report_type in ('get_trade_report','get_waiver_report'):
        focused = [(t,r) for t,r in groups if normalized(t.team_name) in normalized(report)]
        if focused: groups = focused
    if requested_player_ids is not None:
        groups = [(t, [p for p in roster if str(getattr(p, 'playerId', '')) in requested_player_ids])
                  for t, roster in groups]
        groups = [(t, roster) for t, roster in groups if roster]
    context['league_rules'] = evidence.rules(league) if requested_player_ids is None else {}
    context['league_history'] = evidence.history(league, [t for t,_ in groups], week,
        include_performance=report_type in ('get_standings', 'get_power_rankings', 'get_playoffs')) if requested_player_ids is None else {}
    if requested_player_ids is None and not historical:
        from gamedaybot.espn.league_tools import build_schedule_awareness
        from gamedaybot.espn.transaction_tools import build_trade_awareness
        try:
            context['schedule_awareness'] = build_schedule_awareness(league, week=week)
        except Exception:
            context['schedule_awareness'] = {'status': 'Upcoming schedule unavailable; do not invent opponents.'}
        year, league_id = getattr(league, 'year', None), getattr(league, 'league_id', None)
        if (type(year) is int and 2018 <= year <= 2100 and
                str(league_id).isdigit() and len(str(league_id)) <= 20 and
                hasattr(league, 'espn_request')):
            # Independent of optional model tool use: every current report gets
            # a bounded look at upcoming opponents and trade states. No caller
            # should confuse an accepted offer with a completed roster change.
            context['trade_awareness'] = build_trade_awareness(league, deadline=time.monotonic() + 8)
    games = {}
    if not historical and isinstance(getattr(league,'year',None),int):
        try:
            games = nfl_games(league)
            relevant_nfl_teams = {getattr(p,'proTeam',None) for _,roster in groups for p in roster}
            context['nfl_games'] = {k:{'kickoff':datetime.fromtimestamp(v['start'],timezone.utc).isoformat(),
                                       'state':v['state'],'completed':v['completed'],
                                       'lineup_locked':v['state']!='pre' or v['start']<=now.timestamp()} for k,v in games.items() if k in relevant_nfl_teams}
            context['game_state_fetched_at'] = now.isoformat()
        except Exception:
            context['game_state_status'] = 'Unavailable; never infer that a player can still be started.'
    try:
        if requested_player_ids is None:
            if 'trade_awareness' not in context:
                context['league_history']['recent_trades'] = evidence.recent_trade_history(league, {getattr(t,'team_id',None) for t,_ in groups}, week)
            context['league_history']['previous_forecasts'] = evidence.forecast_memory(league, box_scores, games, week)
    except Exception:
        context['league_history']['memory_status'] = 'Trade/forecast history unavailable'
    context['roster_columns'] = ['id','name','position','slot','points','projection','status','nfl_team']
    context['rosters'] = []
    context['fantasy_teams'] = fantasy_team_context(league)
    context['manager_scope'] = 'Current ESPN team managers at fetch time; past management and Discord identities are unknown.'
    context['matchups'] = []
    selected_team_names = {t.team_name for t,_ in groups}
    for box in box_scores or []:
        if not any(getattr(getattr(box,s+'_team',None),'team_name',None) in selected_team_names for s in ('home','away')):
            continue
        sides=[]
        for side in ('home','away'):
            team=getattr(box,side+'_team',None)
            if team is not None:
                sides.append({'team':team.team_name,'abbreviation':getattr(team,'team_abbrev',None),
                              'score':number(getattr(box,side+'_score',None))})
        if len(sides)==2: context['matchups'].append({'week':week,'sides':sides,'completed_week':historical})
    candidates = []
    for team, roster in groups:
        owner = str(getattr(team,'team_name','Unknown'))[:100]
        if historical and box_scores is None:
            owner = 'Historical ownership unknown'
        compact, entries = [], []
        for player in roster:
            name = getattr(player,'name',None)
            if not isinstance(name,str) or not name: continue
            stats = getattr(player,'stats',{}).get(week,{})
            slot = getattr(player,'slot_position',getattr(player,'lineupSlot',None)) if box_scores is not None or not historical else None
            entry = {'id':getattr(player,'playerId',None),'name':name[:100],'fantasy_team':owner,
                     'position':getattr(player,'position',None),'points':number(stats.get('points')),
                     'projected_points':number(stats.get('projected_points'))}
            if slot is not None: entry['slot'] = slot
            if not historical: entry['current_status'] = getattr(player,'injuryStatus',None) or 'UNKNOWN'
            pro_team = getattr(player,'proTeam',None)
            entry['nfl_team'] = pro_team
            if pro_team in context.get('nfl_games',{}):
                entry['game'] = {k:v for k,v in context['nfl_games'][pro_team].items() if k!='kickoff'}
            breakdown = stats.get('breakdown',{})
            keys = ('passingYards','passingTouchdowns','passingInterceptions','rushingAttempts','rushingYards',
                    'rushingTouchdowns','receivingTargets','receivingYards','receivingTouchdowns','receivingReceptions','lostFumbles')
            entry['stats'] = {k:number(breakdown[k]) for k in keys if k in breakdown and number(breakdown[k]) is not None}
            if isinstance(week,int) and isinstance(period,int):
                entry['trend'] = evidence.trends(player,week,period+1 if entry.get('game',{}).get('completed') else period)
                entry['previous_weeks'] = [{'week':w,'points':number(getattr(player,'stats',{}).get(w,{}).get('points'))}
                    for w in range(max(1,week-3),week) if number(getattr(player,'stats',{}).get(w,{}).get('points')) is not None]
            if report_type == 'get_trade_report' and not historical:
                entry['season_avg_points'] = number(getattr(player,'avg_points',None))
            if requested_player_ids is None and slot not in (None,'BE','BN','IR') and entry['points'] is not None:
                total=sum((getattr(p,'points',0) or 0) for p in roster if getattr(p,'slot_position','BE') not in ('BE','BN','IR'))
                if total>0: entry['share_of_starter_points_pct']=round(entry['points']/total*100,1)
            compact.append([entry['id'],entry['name'],entry['position'],slot,entry['points'],entry['projected_points'],
                            entry.get('current_status'),pro_team])
            # Detailed entries prioritize involved players; surrounding rosters stay complete above.
            entry['_priority'] = (normalized(name) in normalized(report),
                                  slot not in (None,'BE','IR','BN') and entry.get('game',{}).get('state') in ('pre','in'),
                                  slot not in (None,'BE','IR','BN') and entry.get('current_status') in ('OUT','DOUBTFUL','QUESTIONABLE'),
                                  slot not in (None,'BE','IR','BN'),entry['points'] or 0)
            entries.append(entry)
        context['rosters'].append({'team':owner,'abbreviation':getattr(team,'team_abbrev',None),'players':compact,
                                   'as_of':'Report week lineup' if box_scores is not None else 'Current roster; historical ownership unknown' if historical else 'Current roster'})
        entries.sort(key=lambda e:e['_priority'],reverse=True)
        candidates.append(entries)
    if trade_actions:
        sides = {}
        for team,action,player,*details in trade_actions:
            completed_at = details[-1] if details and isinstance(details[-1],str) else None
            key = str(completed_at)+':'+str(getattr(team,'team_id',getattr(team,'team_name','Unknown')))
            side = sides.setdefault(key,{'team':team.team_name,'completed_at':completed_at,'sent':[],'received':[]})
            side['received' if action=='TRADE_RECEIVED' else 'sent'].append(str(getattr(player,'name',player)))
            if not any(getattr(player,'name',None)==e['name'] for g in candidates for e in g):
                stats = getattr(player,'stats',{}).get(week,{})
                candidates.insert(0,[{'id':getattr(player,'playerId',None),'name':str(getattr(player,'name',player)),
                    'fantasy_team':'Not found in current rosters','position':getattr(player,'position',None),
                    'points':number(stats.get('points')),'projected_points':number(stats.get('projected_points')),
                    'trend':evidence.trends(player,week,period),'_priority':(True,True,True,0)}])
        context['trade_sides'] = list(sides.values())
        context['current_roster_depth'] = []
        for team,roster in groups:
            positions={}
            for p in roster:
                bucket=positions.setdefault(str(getattr(p,'position','Unknown')),{'count':0,'players':[]})
                bucket['count']+=1
                bucket['players'].append(str(getattr(p,'name','Unknown'))[:100])
            context['current_roster_depth'].append({'team':team.team_name,'positions':positions})
    all_entries=[e for g in candidates for e in g]
    all_names=list(dict.fromkeys(e['name'] for e in all_entries))
    limit = 40 if len(groups)<=2 else 24
    for rank in range(max((len(g) for g in candidates),default=0)):
        for group in candidates:
            if rank<len(group) and len(context['players'])<limit:
                entry=group[rank]
                entry.pop('_priority',None)
                context['players'].append(entry)
    # Named transaction players always lead the detail packet.
    if report_type in ('get_trade_report','get_waiver_report'):
        context['players'].sort(key=lambda e:normalized(e['name']) not in normalized(report))
    context['omitted_player_count'] = len(all_entries)-len(context['players'])
    try:
        completed_weeks = {str(e['id']):week if historical or e.get('game',{}).get('completed') else max(0,week-1)
                           for e in context['players'] if e.get('id') is not None} if isinstance(week,int) else {}
        usage,availability = usage_context(getattr(league,'year',None),[e['id'] for e in context['players'] if e.get('id') is not None],
                                           week, completed_weeks)
        context['usage_status'] = availability
        for entry in context['players']:
            if str(entry.get('id')) not in usage or entry.get('position') in ('K','D/ST'):
                continue
            detail = usage[str(entry['id'])]
            # Drop irrelevant zero categories, e.g. passing yards for a wide receiver.
            keep = {'week','offense_snaps','offense_snap_pct','fumbles_lost_total'}
            if entry.get('position')=='QB':
                keep.update(('passing_yards','passing_tds','passing_interceptions','carries','rushing_yards','rushing_tds'))
            else:
                keep.update(('targets','receptions','target_share_pct','receiving_yards','receiving_tds'))
                if entry.get('position')=='RB': keep.update(('carries','rushing_yards','rushing_tds'))
            detail['weeks']=[{k:v for k,v in row.items() if k in keep or v!=0} for row in detail['weeks']]
            entry['usage'] = detail
    except Exception:
        context['usage_status'] = {'status':'Unavailable; do not infer workload or snap share'}
    if historical:
        context['news_status'] = 'Not fetched: current news and statuses are excluded from historical recaps.'
    elif all_names:
        try:
            context['news'],context['news_fetched_at'] = relevant_news(all_names,now)
            context['news_status'] = 'Dated ESPN highlight briefs; no full articles' if context['news'] else 'No matching recent briefs; not proof of no news.'
        except (requests.RequestException,ValueError,TypeError,KeyError,AttributeError):
            context['news_status'] = 'News unavailable; do not infer player health or availability.'
    else:
        context['news_status'] = 'No player context available.'
    context['highlights'] = []
    for entry in context['players']:
        points,projected = entry['points'],entry['projected_points']
        if points is not None and projected is not None and (historical or entry.get('game',{}).get('state') in ('in','post')):
            entry['vs_projection'] = round(points-projected,2)
            context['highlights'].append({'kind':'performance','player':entry['name'],'team':entry['fantasy_team'],
                'week':week,'points':points,'vs_projection':entry['vs_projection'],
                'final':historical or entry.get('game',{}).get('completed',False)})
    context['highlights'].sort(key=lambda e:abs(e['vs_projection']),reverse=True)
    context['highlights']=context['highlights'][:6]
    # Completed team explanations, identity and compact awareness outrank
    # optional rule catalogs, forecast archives and roster detail.
    default_budget = 16000 if len(groups) <= 2 else 24000
    try: budget=max(16000,min(48000,int(os.environ.get('AI_CONTEXT_CHAR_LIMIT',str(default_budget)))))
    except ValueError: budget=default_budget
    return _prune_context(context, budget, report_type)

def source_notes(context):
    if not context:
        return ''
    lines = ['Research Sources', 'ESPN Fantasy player snapshot fetched ' + context['fetched_at'],
             'Selected player context; missing data is unknown. Status update times are not supplied by ESPN.',
             context['news_status']]
    if context.get('news_fetched_at'):
        lines.append('News feed fetched ' + context['news_fetched_at'])
    for article in context['news']:
        lines.append(article['headline'] + ' — Published ' + article['published_at'] + '\n' + article['url'])
    return '\n'.join(lines)
