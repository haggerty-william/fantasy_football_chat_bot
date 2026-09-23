"""Calculated league research. No model-generated arithmetic or roster mutations."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import random
import statistics
import time

from gamedaybot.espn.analyst_evidence import finite, rules
from gamedaybot.espn.community_state import state, scope


def tool(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties or {},
                       'required': required or [], 'additionalProperties': False}}}


TEAM = {'team_id': {'type': 'string', 'description': 'Exact key from research_context.teams.'}}
TOOLS = [
    tool('simulate_trade_impact', 'Compare optimal projected lineups before/after the most recent completed trade in this report. Counterfactual using current rosters, not reconstructed historical lineups.'),
    tool('find_available_replacements', 'Find unrostered/waiver candidates and projected lineup gains for a fantasy team. This never adds, drops or claims players.', TEAM, ['team_id']),
    tool('get_workload_changes', 'Calculate latest versus prior completed-game targets, carries and snap-share changes. Missing data is unknown.',
         {'player_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3}}, ['player_ids']),
    tool('get_schedule_outlook', 'Get the next four NFL opponents, verified byes and fantasy opponents for a fantasy team. No invented opponent difficulty.', TEAM, ['team_id']),
    tool('simulate_playoff_odds', 'Estimate playoff chances from completed scores and remaining schedule. Returns limitations or unavailable for unsupported rules. Never establishes a clinch.'),
    tool('review_previous_predictions', 'Retrieve saved AI commentary with subsequent completed matchup results. Exact past words, not invented receipts; no automatic grading of subjective claims.'),
]
NAMES = {t['function']['name'] for t in TOOLS}
_free_cache = {}


def projection(player, week):
    value = getattr(player, 'stats', {}).get(week, {}).get('projected_points')
    return float(value) if finite(value) else None


def lineup(roster, slots, week):
    """Exact maximum-weight assignment to eligible slots using a bitmask DP."""
    active = [slot for slot, count in (slots or {}).items() if slot not in ('BE', 'BN', 'IR')
              for _ in range(count)]
    if not active or len(active) > 12:
        return {'available': False, 'reason': 'Verified lineup slots unavailable or exceed supported size.'}
    best = {0: (0.0, [])}
    seen, missing = set(), []
    for player in roster:
        pid = str(player.playerId)
        if pid in seen: continue
        seen.add(pid)
        value = projection(player, week)
        if value is None:
            missing.append(player.name)
            continue
        if getattr(player, 'injuryStatus', '') in ('OUT', 'INJURY_RESERVE', 'SUSPENSION'):
            continue
        eligibility = getattr(player, 'eligibleSlots', [])
        for mask, (score, chosen) in list(best.items()):
            for index, slot in enumerate(active):
                if mask & (1 << index) or slot not in eligibility: continue
                target = mask | (1 << index)
                candidate = (score + value, chosen + [{'id': pid, 'name': player.name, 'slot': slot, 'projection': value}])
                if target not in best or candidate[0] > best[target][0]: best[target] = candidate
    full = (1 << len(active)) - 1
    mask = full if full in best else max(best, key=lambda k: (k.bit_count(), best[k][0]))
    score, chosen = best[mask]
    return {'available': mask == full and not missing, 'projected_points': round(score, 2),
            'filled_slots': mask.bit_count(), 'required_slots': len(active), 'lineup': chosen,
            'missing_projections': missing, 'note': 'Theoretical lineup; ignores transaction timing and already-locked slots. Not start/sit instructions.'}


def bye(player):
    weeks = {int(w) for w in getattr(player, 'schedule', {}) if str(w).isdigit()}
    # Only infer a bye from a complete 17-game, 18-week regular-season schedule.
    return next(iter(set(range(1, 19)) - weeks)) if len(weeks) == 17 and weeks <= set(range(1, 19)) else None


def depth(roster):
    return {'position_counts': dict(Counter(getattr(p, 'position', 'Unknown') for p in roster)),
            'bye_conflicts': [{'week': w, 'players': [p.name for p in roster if bye(p) == w]}
                             for w, count in sorted(Counter(bye(p) for p in roster if bye(p)).items()) if count > 1]}


def trade_impact(league, context, week):
    sides = context.get('trade_sides', [])
    if not sides: return {'error': 'No completed trade in this report.'}
    latest = max(str(s.get('completed_at') or '') for s in sides)
    sides = [s for s in sides if str(s.get('completed_at') or '') == latest]
    teams = {t.team_name: t for t in league.teams}
    players = {}
    for t in league.teams:
        for p in t.roster: players.setdefault(p.name, []).append(p)
    slots = rules(league).get('lineup_slots')
    result = []
    for side in sides:
        team = teams.get(side['team'])
        names = side['sent'] + side['received']
        if team is None or any(len(players.get(n, [])) != 1 for n in names):
            return {'error': 'Trade participants cannot be uniquely resolved in current rosters.'}
        current = {p.name for p in team.roster}
        if not set(side['received']) <= current or set(side['sent']) & current:
            return {'error': 'Subsequent roster moves prevent a reliable counterfactual for this trade.'}
        before = [p for p in team.roster if p.name not in side['received']] + [players[n][0] for n in side['sent']]
        a, b = lineup(before, slots, week), lineup(team.roster, slots, week)
        result.append({'team': team.team_name, 'sent': side['sent'], 'received': side['received'],
                       'before': a, 'after': b, 'starter_projection_change': round(b['projected_points'] - a['projected_points'], 2)
                       if a['available'] and b['available'] else None,
                       'depth_before': depth(before), 'depth_after': depth(team.roster)})
    return {'kind': 'Counterfactual estimate', 'week': week, 'completed_at': latest, 'teams': result,
            'limitations': 'Current rosters and report-week ESPN projections, not trade-time rosters or rest-of-season value. Empty/missing projections prevent a gain estimate. No roster actions performed.'}


def replacements(league, team, week):
    key = (scope(league), week)
    cached = _free_cache.get(key)
    if cached and time.monotonic() - cached[0] < 600:
        players = cached[1]
    else:
        players = league.free_agents(week=week, size=60)
        _free_cache[key] = (time.monotonic(), players)
    owned = {str(p.playerId) for t in league.teams for p in t.roster}
    slots = rules(league).get('lineup_slots')
    baseline = lineup(team.roster, slots, week)
    candidates = []
    for p in players:
        if str(p.playerId) in owned or projection(p, week) is None: continue
        if getattr(p, 'injuryStatus', '') in ('OUT', 'INJURY_RESERVE', 'SUSPENSION'): continue
        candidates.append(p)
    candidates.sort(key=lambda p: projection(p, week), reverse=True)
    rows = []
    for p in candidates[:60]:
        improved = lineup([*team.roster, p], slots, week)
        comparable = [r for r in baseline.get('lineup', []) if r['slot'] in p.eligibleSlots]
        weakest = min(comparable, key=lambda r: r['projection']) if comparable else None
        rows.append({'id': str(p.playerId), 'name': p.name, 'position': p.position,
                     'eligible_slots': p.eligibleSlots, 'projected_points': projection(p, week),
                     'current_status': getattr(p, 'injuryStatus', 'UNKNOWN'),
                     'lowest_eligible_starter': weakest,
                     'projection_gap_to_that_starter': round(projection(p, week)-weakest['projection'],2) if weakest else None,
                     'starter_projection_gain': round(improved['projected_points'] - baseline['projected_points'], 2)
                     if baseline['available'] and improved['available'] else None,
                     'displaced_starters': [x['name'] for x in baseline.get('lineup', [])
                                           if x['id'] not in {r['id'] for r in improved.get('lineup', [])}]})
    rows.sort(key=lambda r: (r['starter_projection_gain'] or 0,
                            r['projection_gap_to_that_starter'] if r['projection_gap_to_that_starter'] is not None else float('-inf'),
                            r['projected_points']), reverse=True)
    return {'team': team.team_name, 'week': week, 'candidates': rows[:5],
            'limitations': 'Sample of up to 60 ESPN available/waiver players, ranked by ownership before projection. Waiver priority, FAAB, required drops and lineup locks are not modeled. Gains assume an open roster spot; not an acquisition guarantee.'}


def workload(packet):
    result = []
    for p in packet.get('players', []):
        rows = sorted(p.get('usage', {}).get('weeks', []), key=lambda r: r['week'])
        metrics = {}
        for field in ('targets', 'carries', 'offense_snap_pct', 'target_share_pct'):
            values = [r for r in rows if finite(r.get(field))]
            if len(values) < 2: continue
            latest, previous = values[-1], values[:-1]
            mean = statistics.mean(r[field] for r in previous)
            metrics[field] = {'latest_week': latest['week'], 'latest': latest[field],
                              'prior_weeks': [r['week'] for r in previous], 'prior_average': round(mean, 2),
                              'change': round(latest[field] - mean, 2),
                              'units': 'percentage points' if field.endswith('_pct') else 'per-game count'}
        result.append({'id': p['id'], 'name': p['name'], 'changes': metrics,
                       'sample_games': len(rows)})
    return {'players': result, 'limitations': 'Completed-game samples only. Gaps and missing metrics are unknown. Small samples do not establish a sustained role; share changes are percentage points.'}


def schedule_outlook(league, team, week):
    weeks = list(range(week, min(18, week + 3) + 1))
    fantasy = []
    periods = getattr(league.settings, 'matchup_periods', {})
    for period, scoring_weeks in periods.items():
        index = int(period) - 1
        if not set(weeks) & set(scoring_weeks): continue
        opponent = team.schedule[index] if index < len(team.schedule) else None
        fantasy.append({'matchup_period': int(period), 'weeks': scoring_weeks,
                        'opponent': getattr(opponent, 'team_name', 'Unknown')})
    players = []
    for p in team.roster:
        schedule = getattr(p, 'schedule', {})
        players.append({'id': str(p.playerId), 'name': p.name, 'bye_week': bye(p),
                        'opponents': [{'week': w, 'opponent': schedule.get(w, schedule.get(str(w), {})).get('team'),
                                       'status': 'bye' if bye(p) == w else 'scheduled' if w in schedule or str(w) in schedule else 'unknown'} for w in weeks]})
    return {'team': team.team_name, 'fantasy_matchups': fantasy, 'players': players,
            'bye_conflicts': [r for r in depth(team.roster)['bye_conflicts'] if r['week'] in weeks],
            'limitations': 'ESPN schedule snapshot. Opponent names alone do not establish matchup difficulty. Missing games are unknown unless a complete schedule establishes a bye.'}


def playoff_odds(league, week, iterations=600):
    settings, teams = league.settings, league.teams
    regular, places = settings.reg_season_count, settings.playoff_team_count
    periods = getattr(settings, 'matchup_periods', {})
    tie_rule = getattr(settings, 'playoff_seed_tie_rule', '')
    if (not periods or getattr(settings, 'median_scoring', False)
            or any(periods.get(str(w), periods.get(w)) != [w] for w in range(1, regular + 1))
            or tie_rule not in ('TOTAL_POINTS_SCORED', 'TOTAL_POINTS')
            or getattr(settings, 'tie_rule', 'NONE') != 'NONE'):
        return {'error': 'Simulation unavailable for median scoring, multi-week matchups, or unsupported matchup/seeding tiebreakers.'}
    if not 1 <= places <= len(teams) or week > regular:
        return {'error': 'No supported remaining regular season to simulate.'}
    by_id = {t.team_id: t for t in teams}
    divisions = {}
    for t in teams: divisions.setdefault(getattr(t, 'division_id', 0), []).append(t.team_id)
    if len(divisions) > places: return {'error': 'Division qualification rules cannot be represented.'}
    scores, wins, points = {}, {}, {}
    for t in teams:
        completed = [(t.scores[i], t.outcomes[i]) for i in range(min(week-1, len(t.scores), len(t.outcomes)))
                     if t.outcomes[i] in ('W', 'L', 'T') and finite(t.scores[i])]
        if len(completed) != week-1:
            return {'error': 'Completed regular-season results are missing.'}
        scores[t.team_id] = [s for s, _ in completed]
        wins[t.team_id] = sum(1 if o == 'W' else .5 if o == 'T' else 0 for _, o in completed)
        points[t.team_id] = sum(scores[t.team_id])
    all_scores = [s for rows in scores.values() for s in rows]
    if not all_scores: return {'error': 'At least one completed week is needed for scoring estimates.'}
    pairs = {}
    for w in range(week, regular+1):
        seen, games = set(), []
        for t in teams:
            if t.team_id in seen: continue
            opp = t.schedule[w-1] if w <= len(t.schedule) else None
            oid = getattr(opp, 'team_id', None)
            if (oid not in by_id or oid == t.team_id or oid in seen or w > len(opp.schedule)
                    or getattr(opp.schedule[w-1], 'team_id', None) != t.team_id):
                return {'error': 'Remaining schedule is incomplete, contains byes or is not reciprocal.'}
            seen.update((t.team_id, oid)); games.append((t.team_id, oid))
        pairs[w] = games
    center = statistics.mean(all_scores)
    spread = max(statistics.pstdev(all_scores), abs(center)*.20, 1)
    means = {tid: (sum(s)+3*center)/(len(s)+3) for tid, s in scores.items()}
    sigmas = {tid: max(statistics.pstdev(s) if len(s)>1 else spread, spread*.75) for tid, s in scores.items()}
    rng = random.Random(f'{league.league_id}:{league.year}:{week}')
    counts = Counter()
    conditional = {tid: {'win': [0, 0], 'lose': [0, 0]} for tid in by_id}
    for _ in range(iterations):
        wns, pts, first = dict(wins), dict(points), {}
        for w, games in pairs.items():
            for a, b in games:
                sa, sb = (round(rng.gauss(means[t], sigmas[t]), 2) for t in (a, b))
                pts[a] += sa; pts[b] += sb
                wns[a] += 1 if sa>sb else .5 if sa==sb else 0
                wns[b] += 1 if sb>sa else .5 if sa==sb else 0
                if w == week and sa != sb:
                    first[a], first[b] = ('win', 'lose') if sa>sb else ('lose', 'win')
        rank = lambda t: (wns[t], pts[t], rng.random())
        leaders = [max(ids, key=rank) for ids in divisions.values()] if len(divisions)>1 else []
        selected = set(leaders + sorted((t for t in by_id if t not in leaders), key=rank, reverse=True)[:places-len(leaders)])
        counts.update(selected)
        for tid, outcome in first.items():
            conditional[tid][outcome][0] += int(tid in selected)
            conditional[tid][outcome][1] += 1
    return {'kind': 'Monte Carlo estimate, not a clinch calculation', 'simulations': iterations,
            'completed_weeks': week-1, 'playoff_places': places,
            'teams': [{'team': t.team_name, 'playoff_pct': round(100*counts[t.team_id]/iterations, 1),
                       'conditional_on_this_week': {k: round(100*v[0]/v[1],1) if v[1] else None for k,v in conditional[t.team_id].items()}}
                      for t in teams],
            'assumptions': 'Independent normal scores; recent team means shrunk toward league mean with three pseudo-games; uncertainty floor from league score spread. Division winners qualify, then win totals and points scored. Exact residual ties randomized. Current week simulated from scratch; live scores, injuries and future roster changes not modeled. Conditional odds are associations, not causal effects. Early-season estimates are especially uncertain.'}


def archive_commentary(league, context, week, report_type, model, text):
    """Save generated, validated commentary; never claim it was delivered."""
    if not league or not context or not isinstance(week, int) or context.get('historical'): return
    data = {'generated_at': datetime.now(timezone.utc).isoformat(), 'report_type': report_type,
            'model': model, 'text': text, 'teams': [t.team_id for t in league.teams
                if any(r.get('team') == t.team_name for r in context.get('rosters', []))],
            'kind': 'Generated AI commentary; delivery not tracked',
            'game_states_at_generation': {k: v.get('state') for k, v in context.get('nfl_games', {}).items()}}
    key = hashlib.sha256(json.dumps([week, report_type, text]).encode()).hexdigest()
    with state() as db:
        db.execute('CREATE TABLE IF NOT EXISTS ai_commentary_archive(scope TEXT,key TEXT,week INTEGER,data TEXT,PRIMARY KEY(scope,key))')
        db.execute('INSERT OR IGNORE INTO ai_commentary_archive VALUES (?,?,?,?)', (scope(league), key, week, json.dumps(data)))


def review_predictions(league, week):
    with state() as db:
        db.execute('CREATE TABLE IF NOT EXISTS ai_commentary_archive(scope TEXT,key TEXT,week INTEGER,data TEXT,PRIMARY KEY(scope,key))')
        rows = db.execute('SELECT week,data FROM ai_commentary_archive WHERE scope=? AND week<? ORDER BY week DESC,rowid DESC LIMIT 3',
                          (scope(league), min(week+1, league.scoringPeriodId))).fetchall()
    periods = getattr(league.settings, 'matchup_periods', {})
    result = []
    for w, raw in rows:
        data = json.loads(raw)
        outcomes = []
        for period, weeks in periods.items():
            if w not in weeks or max(weeks) >= league.scoringPeriodId: continue
            index = int(period)-1
            for t in league.teams:
                if t.team_id not in data['teams'] or index >= len(t.outcomes) or index >= len(t.scores): continue
                if t.outcomes[index] in ('W','L','T'):
                    outcomes.append({'team': t.team_name, 'matchup_period': int(period),
                                     'score': t.scores[index], 'result': t.outcomes[index]})
        result.append({'week': w, **data, 'subsequent_completed_results': outcomes})
    return {'receipts': result, 'limitations': 'Archive begins with this feature. Exact generated words, not proof of posting. In-progress commentary is labeled by game states; retrospective text is not a pregame prediction. Outcomes do not establish whether a trade caused a result. No automated accuracy grade for subjective claims.'}


def execute(name, args, researcher):
    league, context, week = researcher.league, researcher.context, researcher.week
    if context.get('historical') and name not in ('get_workload_changes', 'review_previous_predictions'):
        return {'error': 'Current roster and forward-looking tools are excluded from historical recaps.'}
    expected = {'team_id'} if name in ('find_available_replacements', 'get_schedule_outlook') else {'player_ids'} if name == 'get_workload_changes' else set()
    if not isinstance(args, dict) or set(args) != expected: return {'error': 'Invalid tool arguments.'}
    team = None
    if 'team_id' in args:
        if not isinstance(args['team_id'], str): return {'error': 'Use an exact fantasy team ID.'}
        team = next((t for t in league.teams if str(t.team_id) == args['team_id']), None)
        if team is None: return {'error': 'Unknown fantasy team ID.'}
    if name == 'get_workload_changes':
        packet = researcher.execute('get_player_stats', json.dumps(args))
        return packet if 'error' in packet else workload(packet)
    if name == 'simulate_trade_impact': return trade_impact(league, context, week)
    if name == 'find_available_replacements': return replacements(league, team, week)
    if name == 'get_schedule_outlook': return schedule_outlook(league, team, week)
    if name == 'simulate_playoff_odds': return playoff_odds(league, week)
    if name == 'review_previous_predictions': return review_predictions(league, week)
    return {'error': 'Unknown tool.'}
