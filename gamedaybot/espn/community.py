"""Evidence-based community reports; calculations never depend on an LLM."""
from datetime import datetime, timezone
from decimal import Decimal
import requests
from gamedaybot.espn import functionality as espn
from gamedaybot.espn.community_state import state, scope

BENCH = {'BE', 'IR', 'BN'}


def starters(lineup):
    return [p for p in lineup if getattr(p, 'slot_position', 'BE') not in BENCH]


def valid_boxes(boxes):
    return [b for b in boxes if getattr(b.home_team, 'team_id', None) and getattr(b.away_team, 'team_id', None)
            and b.home_team.team_id != b.away_team.team_id]


def nfl_games(league):
    """Use actual ESPN event states, not espn-api's estimated three-hour clock."""
    response = requests.get('https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
                            params={'dates': league.year, 'seasontype': 2, 'week': league.scoringPeriodId, 'limit': 100}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if (data.get('season', {}).get('year') != league.year or data.get('season', {}).get('type') != 2
            or data.get('week', {}).get('number') != league.scoringPeriodId):
        raise ValueError('NFL schedule does not match the requested fantasy week')
    games = {}
    for event in data.get('events', []):
        start = datetime.fromisoformat(event['date'].replace('Z', '+00:00')).timestamp()
        status = event['status']['type']
        for competition in event.get('competitions', []):
            for competitor in competition.get('competitors', []):
                abbr = competitor['team']['abbreviation'].upper()
                abbr = {'WAS': 'WSH', 'LA': 'LAR'}.get(abbr, abbr)
                games[abbr] = {'start': start, 'state': status['state'], 'completed': status.get('completed', False)}
    return games


def preview(league, boxes):
    candidates = []
    games = {}
    if isinstance(getattr(league, 'year', None), int):
        try:
            games = nfl_games(league)
        except (requests.RequestException, ValueError, KeyError, AttributeError):
            pass
    for b in valid_boxes(boxes):
        if not b.home_lineup or not b.away_lineup:
            continue
        active = starters(b.home_lineup) + starters(b.away_lineup)
        if games and active and all(games.get(getattr(p,'proTeam',None),{}).get('completed') or getattr(p,'on_bye_week',False) for p in active):
            continue
        h = getattr(b, 'home_projected', None)
        a = getattr(b, 'away_projected', None)
        if not isinstance(h,(int,float)) or h<0:
            h = sum(getattr(p, 'projected_points', 0) or 0 for p in starters(b.home_lineup))
        if not isinstance(a,(int,float)) or a<0:
            a = sum(getattr(p, 'projected_points', 0) or 0 for p in starters(b.away_lineup))
        candidates.append((abs(h-a), b, h, a))
    if not candidates:
        return 'Rivalry preview\nNo unfinished matchup with available projections. Use /recap for completed weeks.'
    _, b, h, a = min(candidates, key=lambda x:x[0])
    home, away = b.home_team, b.away_team
    lines = ['Rivalry preview', 'The Neighborhood Grudge Match',
             f'{home.team_name} vs {away.team_name}',
             f'Current score: {b.home_score:.2f} - {b.away_score:.2f}',
             f'Projected finish: {h:.2f} - {a:.2f} | Closest projected unfinished matchup.',
             f'Forecast: {home.team_name if h > a else away.team_name} by {abs(h-a):.2f} (estimate).' if h != a else 'Forecast: a projected dead heat.',
             f'Seeds: #{home.standing} vs #{away.standing} | {league.settings.playoff_team_count} playoff places.']
    meetings = []
    for i, opponent in enumerate(getattr(home, 'schedule', [])):
        if i >= league.scoringPeriodId-1:
            break
        if getattr(opponent, 'team_id', opponent) == away.team_id and i < len(home.scores) and i < len(away.scores):
            if home.scores[i] is not None and away.scores[i] is not None:
                meetings.append(f'Week {i+1}: {home.team_name} {home.scores[i]:.2f} - {away.scores[i]:.2f} {away.team_name}')
    lines += ['Previous meetings this season:'] + meetings[-2:] if meetings else ['First meeting this season. Prior seasons are not included.']
    cutoff = league.settings.playoff_team_count
    if abs(home.standing-cutoff) <= 1 or abs(away.standing-cutoff) <= 1:
        lines.append('Bubble watch: this matchup involves a team near the current playoff cutoff; it is not a clinching scenario.')
    lines.append('Make your picks with /pickem before the first NFL kickoff of the week.')
    return '\n'.join(lines)


def monday_watch(league, boxes, games):
    lines = ['Monday night watch', 'Remaining means scheduled or in progress on ESPN; stat corrections can change scores.']
    for b in valid_boxes(boxes):
        remaining = []
        for side in ('home','away'):
            active = [p for p in starters(getattr(b, side+'_lineup')) if games.get(p.proTeam, {}).get('state') in ('pre','in')]
            remaining.append(active)
        if not any(remaining):
            continue
        lines += [f'{b.home_team.team_name} vs {b.away_team.team_name}', f'Live score: {b.home_score:.2f} - {b.away_score:.2f}']
        for side, players in zip(('home','away'),remaining):
            team = getattr(b, side+'_team')
            lines.append(f"{team.team_name}: {', '.join(p.name for p in players) or 'No starters remaining'}")
        deficit = abs(Decimal(str(b.home_score))-Decimal(str(b.away_score)))
        if deficit:
            trailing = b.home_team if b.home_score < b.away_score else b.away_team
            lines.append(f'{trailing.team_name} needs {deficit + Decimal("0.01"):.2f} MORE points than its opponent from here to take the lead.')
        else:
            lines.append('Tied: the next net point takes the lead.')
        lines.append('')
    return '\n'.join(lines) if len(lines)>2 else ''


def awards(league, boxes, week):
    boxes = valid_boxes(boxes)
    if not boxes:
        return ''
    lines = ['Neighborhood awards', f'Week {week} | Completed scores; lineup comparisons use hindsight.']
    swaps, losses, margins, upsets = [], [], [], []
    for b in boxes:
        if b.home_score != b.away_score:
            winner, loser = (b.home_team,b.away_team) if b.home_score>b.away_score else (b.away_team,b.home_team)
            margins.append((abs(b.home_score-b.away_score),winner,loser))
            losses.append((min(b.home_score,b.away_score),loser))
        projected = [sum(getattr(p,'projected_points',0) or 0 for p in starters(getattr(b,s+'_lineup'))) for s in ('home','away')]
        actual = b.home_score-b.away_score
        if actual*(projected[0]-projected[1])<0:
            winner = b.home_team if actual>0 else b.away_team
            upsets.append((abs(projected[0]-projected[1]), winner))
        for side in ('home','away'):
            lineup = getattr(b,side+'_lineup')
            for bench in [p for p in lineup if p.slot_position in ('BE','BN')]:
                for starter in starters(lineup):
                    gain = bench.points-starter.points
                    if gain>0 and starter.slot_position in getattr(bench,'eligibleSlots',[]):
                        swaps.append((gain,getattr(b,side+'_team'),bench,starter))
    if swaps:
        gain,team,bench,starter = max(swaps,key=lambda x:x[0])
        lines += ['The bench malpractice award:', f'{team.team_name}: {bench.name} ({bench.points:.2f}) over {starter.name} ({starter.points:.2f}) would add {gain:.2f}.', 'An eligible single-player swap, not a reconstructed optimal lineup. The bench apparently hired a better coach.']
    if margins:
        margin,winner,loser = min(margins,key=lambda x:x[0])
        lines += ['Narrowest escape:', f'{winner.team_name} beat {loser.team_name} by {margin:.2f}. A win held together with duct tape.']
    if losses:
        points,team = max(losses,key=lambda x:x[0])
        lines += ['Highest-scoring loser:', f'{team.team_name}: {points:.2f}. All that production, zero bragging rights.']
    if upsets:
        gap,team = max(upsets,key=lambda x:x[0])
        lines += ['Biggest projected upset:', f'{team.team_name} won despite a {gap:.2f}-point projected deficit. Based on ESPN historical projections, which may have changed.']
    else:
        lines.append('No projected upset found in the available data.')
    return '\n'.join(lines)


def playoff_picture(league, compact=False):
    teams = sorted(league.teams,key=lambda t:t.standing)
    slots = league.settings.playoff_team_count
    regular = league.settings.reg_season_count
    # Conservative sufficient bounds. Divisions, median wins and multi-week
    # matchups require additional rules; never claim a clinch in those formats.
    periods = getattr(league.settings, 'matchup_periods', {})
    supported = (len(getattr(league.settings,'division_map',{}))<=1 and not getattr(league.settings,'median_scoring',False)
                 and bool(periods) and all(len(v)==1 for k,v in periods.items() if int(k)<=regular))
    value = lambda t:t.wins + getattr(t,'ties',0)*.5
    remaining = lambda t:max(0,regular-t.wins-t.losses-getattr(t,'ties',0))
    lines = ['Playoff picture', f'{slots} playoff places | ESPN current seeds | Current position is not a clinch or elimination.']
    for t in teams:
        status = 'Inside current playoff places (provisional)' if t.standing<=slots else 'Outside current playoff places (provisional)'
        if supported:
            threats = sum(o is not t and value(o)+remaining(o)>=value(t) for o in teams)
            ahead = sum(o is not t and value(o)>value(t)+remaining(t) for o in teams)
            if threats < slots: status='Clinched by record bound'
            elif ahead >= slots: status='Eliminated by record bound'
        if not compact or 'by record bound' in status or abs(t.standing-slots)<=1:
            lines.append(f'{t.team_name}: #{t.standing} | {status}')
    if not supported:
        lines.append('Clinches/eliminations are unconfirmed: this league uses additional seeding or scoring rules.')
    elif any(remaining(t) for t in teams):
        lines.append('These are sufficient mathematical bounds, not exhaustive scenarios or predicted odds.')
    else:
        lines.append('Final tied-record seeding follows ESPN; bounds alone cannot resolve tiebreakers.')
    bubble = [t for t in teams if abs(t.standing-slots)<=1]
    for t in bubble[:3]:
        index=league.scoringPeriodId-1
        schedule=getattr(t,'schedule',[])
        if index<len(schedule):
            opponent=schedule[index]
            if getattr(opponent,'team_id',None)!=t.team_id and getattr(opponent,'team_name',None):
                lines.append(f'Bubble matchup: {t.team_name} vs {opponent.team_name}')
    return '\n'.join(lines)


def game_key(box):
    return ':'.join(map(str,sorted((box.home_team.team_id,box.away_team.team_id))))


def final_outcome(league, team, week):
    periods = getattr(league.settings, 'matchup_periods', {})
    period = next((int(k) for k, weeks in periods.items() if week in weeks), week)
    weeks = periods.get(str(period), periods.get(period, [week]))
    if max(weeks) >= league.scoringPeriodId:
        return None
    outcomes = getattr(team, 'outcomes', [])
    return outcomes[period-1] if 0 < period <= len(outcomes) else None


def settle_picks(league):
    key=scope(league)
    with state() as db:
        weeks=[r[0] for r in db.execute('SELECT DISTINCT week FROM picks WHERE scope=? AND result IS NULL AND week<?',(key,league.scoringPeriodId))]
        for week in weeks:
            for box in valid_boxes(espn.fetch_box_scores(league,week=week)):
                # ESPN's final matchup outcome is authoritative, not the week clock.
                if final_outcome(league, box.home_team, week) not in ('W','L','T'):
                    continue
                if box.home_score==box.away_score:
                    db.execute('UPDATE picks SET result=0.5 WHERE scope=? AND week=? AND game=?',(key,week,game_key(box)))
                else:
                    winner=box.home_team.team_id if box.home_score>box.away_score else box.away_team.team_id
                    db.execute('UPDATE picks SET result=CASE WHEN team=? THEN 1 ELSE 0 END WHERE scope=? AND week=? AND game=?',(winner,key,week,game_key(box)))


def pickem(league, boxes, user, name, selection=None, games=None):
    settle_picks(league)
    key=scope(league)
    week=league.scoringPeriodId
    if selection is not None:
        periods = getattr(league.settings, 'matchup_periods', {})
        if any(week in weeks and len(weeks)>1 for weeks in periods.values()):
            return 'Pick’em\nNew picks are unavailable for multi-week fantasy matchups. Omit the team option to view the leaderboard.'
        if not games:
            return 'Pick’em\nKickoff data is unavailable. Picks are temporarily locked; try again shortly.'
        deadline=min(g['start'] for g in games.values())
        if datetime.now(timezone.utc).timestamp()>=deadline:
            return 'Pick’em\nPicks locked at the first NFL kickoff of this week. Your existing selections are saved.'
        matches=[b for b in valid_boxes(boxes) if selection.team_id in (b.home_team.team_id,b.away_team.team_id)]
        if len(matches)!=1:
            return 'Pick’em\nThat team has no available matchup this week.'
        with state() as db:
            if datetime.now(timezone.utc).timestamp()>=deadline:
                return 'Pick’em\nPicks are now locked at kickoff. No change was saved.'
            db.execute('INSERT INTO picks VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(scope,week,user,game) DO UPDATE SET team=excluded.team,name=excluded.name,result=NULL',
                       (key,week,str(user),str(name)[:50],game_key(matches[0]),selection.team_id))
    with state() as db:
        picks=dict(db.execute('SELECT game,team FROM picks WHERE scope=? AND week=? AND user=?',(key,week,str(user))))
        leaders=db.execute('SELECT user,MAX(name),SUM(result),COUNT(result) FROM picks WHERE scope=? GROUP BY user HAVING COUNT(result)>0 ORDER BY SUM(result) DESC,COUNT(result),user LIMIT 10',(key,)).fetchall()
    names={t.team_id:t.team_name for t in league.teams}
    lines=['Pick’em',f'Week {week} | One pick per matchup. Change picks until the FIRST NFL kickoff of the week.']
    if selection is not None: lines.append(f'Saved: {selection.team_name}')
    for box in valid_boxes(boxes):
        lines.append(f'{box.home_team.team_name} vs {box.away_team.team_name}: {names.get(picks.get(game_key(box)),"Not picked")}')
    lines += ['', 'Prediction leaderboard:', 'Correct = 1 point | Tied matchup = 0.5 | Unpicked = 0']
    lines += [f'{i}. {n}: {points:g} points / {count} settled picks' for i,(_,n,points,count) in enumerate(leaders,1)] or ['No settled picks yet.']
    return '\n'.join(lines)
