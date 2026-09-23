"""Small, calculated evidence packets for the local analyst."""
from datetime import datetime, timezone, timedelta
import json
import math
import statistics
import time
from threading import Lock
from gamedaybot.espn.community_state import state, scope

_cache={}
_lock=Lock()


def finite(value):
    return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)


def rules(league):
    settings=getattr(league,'settings',None)
    if settings is None: return {'status':'League rules unavailable'}
    raw=getattr(settings,'_raw_scoring_settings',{})
    slots=None
    # Read slot IDs directly: older SDKs zip position labels to a sparse mapping.
    if hasattr(league,'espn_request'):
        key=(getattr(league,'league_id',None),getattr(league,'year',None))
        with _lock: cached=_cache.get(key)
        if cached and time.monotonic()-cached[0]<600:
            slots=cached[1]
        else:
            try:
                from espn_api.football.constant import POSITION_MAP
                data=league.espn_request.get_league()['settings']['rosterSettings']['lineupSlotCounts']
                slots={POSITION_MAP.get(int(k),str(k)):v for k,v in data.items() if v}
                with _lock: _cache[key]=(time.monotonic(),slots)
            except Exception:
                slots=None
    labels={r.get('id'):r for r in getattr(settings,'scoring_format',[])}
    scoring=[]
    for item in raw.get('scoringItems',[]):
        if item.get('points') or item.get('pointsOverrides'):
            label=labels.get(item.get('statId'),{})
            scoring.append({'stat':label.get('label','Unknown scoring category'),'stat_id':item.get('statId'),
                            'points':item.get('points'),**({'position_overrides':item['pointsOverrides']} if item.get('pointsOverrides') else {})})
    return {'scoring':scoring,'lineup_slots':slots,'lineup_slots_status':'Verified ESPN slot IDs' if slots is not None else 'Unknown; do not assume lineup requirements',
            'playoff_places':getattr(settings,'playoff_team_count',None),
            'regular_season_matchups':getattr(settings,'reg_season_count',None),
            'median_scoring':getattr(settings,'median_scoring',None),
            'tie_rule':getattr(settings,'tie_rule',None),'seed_tiebreaker':getattr(settings,'playoff_seed_tie_rule',None)}


def trends(player, week, period):
    stats=getattr(player,'stats',{})
    end=min(week,period-1)
    samples=[]
    for w in range(max(1,end-3),end+1):
        row=stats.get(w,{})
        if finite(row.get('points')):
            # A zero without any stat breakdown may be a bye/inactive/missing row.
            if row['points']==0 and not row.get('breakdown') and not row.get('points_breakdown'):
                continue
            samples.append((w,row['points'],row.get('projected_points')))
    if not samples: return {'sample_games':0,'note':'No verified completed scoring samples'}
    values=[p for _,p,_ in samples]
    differences=[p-proj for _,p,proj in samples if finite(proj)]
    return {'sample_games':len(samples),'weeks':[w for w,_,_ in samples],
            'average_points':round(statistics.mean(values),2),'low':min(values),'high':max(values),
            'population_stddev':round(statistics.pstdev(values),2) if len(values)>1 else None,
            'sample_warning':'Too few games to establish consistency' if len(values)<3 else 'Small recent sample; not a guarantee',
            'mean_vs_projection':round(statistics.mean(differences),2) if differences else None,
            'projection_samples':len(differences),'zero_without_participation_excluded':True}


def history(league, teams, week):
    period=getattr(league,'scoringPeriodId',week)
    periods=getattr(getattr(league,'settings',None),'matchup_periods',{})
    items=[]
    meetings=[]
    seen=set()
    for team in teams:
        outcomes=getattr(team,'outcomes',[])
        scores=getattr(team,'scores',[])
        schedule=getattr(team,'schedule',[])
        completed=[]
        for i,outcome in enumerate(outcomes):
            scoring_weeks=periods.get(str(i+1),periods.get(i+1,[i+1]))
            if max(scoring_weeks)>=period or max(scoring_weeks)>week or outcome not in ('W','L','T'): continue
            completed.append(outcome)
            opponent=schedule[i] if i<len(schedule) else None
            if opponent is not None and opponent in teams and getattr(opponent,'team_id',None)!=getattr(team,'team_id',None):
                key=(i,tuple(sorted((str(getattr(team,'team_id',team.team_name)),str(getattr(opponent,'team_id',opponent.team_name))))))
                other=getattr(opponent,'scores',[])
                if key not in seen and i<len(scores) and i<len(other) and finite(scores[i]) and finite(other[i]):
                    seen.add(key)
                    meetings.append({'matchup_period':i+1,'teams':[team.team_name,opponent.team_name],'scores':[scores[i],other[i]]})
        streak=[]
        for result in reversed(completed):
            if streak and result!=streak[0]: break
            streak.append(result)
        items.append({'team':team.team_name,'completed_record':{k:completed.count(k) for k in ('W','L','T')},
                      'streak':{'result':streak[0] if streak else None,'length':len(streak)}})
    return {'scope':'This season; completed matchup periods through report week', 'teams':items,'meetings':meetings[-8:]}


def recent_trade_history(league, team_ids, week):
    if not hasattr(league,'recent_activity') or not hasattr(league,'year'): return []
    # Never attach today's transaction history to a historical recap.
    if week<getattr(league,'scoringPeriodId',week): return []
    from gamedaybot.espn.trades import completed_trades
    key=('trades',getattr(league,'league_id',None),league.year)
    with _lock: cached=_cache.get(key)
    if cached and time.monotonic()-cached[0]<600:
        rows=cached[1]
    else:
        since=int((datetime.now(timezone.utc)-timedelta(days=30)).timestamp()*1000)
        rows=completed_trades(league,since)
        with _lock: _cache[key]=(time.monotonic(),rows)
    result=[]
    for row in rows:
        if not any(getattr(t,'team_id',None) in team_ids for t,_,_,*_ in row['actions']): continue
        result.append({'completed_at':datetime.fromtimestamp(row['date']/1000,timezone.utc).isoformat(),
                       'received':[{'team':t.team_name,'player':getattr(p,'name',str(p))}
                                   for t,a,p,*_ in row['actions'] if a=='TRADE_RECEIVED']})
    return result[-3:]


def forecast_memory(league, boxes, games, week):
    if not all(hasattr(league,k) for k in ('league_id','year')): return []
    key=scope(league)
    now=datetime.now(timezone.utc).timestamp()
    rows=[]
    with state() as db:
        db.execute('CREATE TABLE IF NOT EXISTS analyst_forecasts(scope TEXT,week INTEGER,game TEXT,data TEXT,PRIMARY KEY(scope,week,game))')
        periods=getattr(getattr(league,'settings',None),'matchup_periods',{})
        single_week=not any(week in weeks and len(weeks)>1 for weeks in periods.values())
        if single_week and boxes and games and week==league.scoringPeriodId and now<min(g['start'] for g in games.values()):
            for b in boxes:
                h,a=getattr(b,'home_team',None),getattr(b,'away_team',None)
                if not h or not a or not hasattr(h,'team_id') or not hasattr(a,'team_id'): continue
                projections=[sum(getattr(p,'projected_points',0) or 0 for p in getattr(b,s+'_lineup',[]) if getattr(p,'slot_position','BE') not in ('BE','BN','IR')) for s in ('home','away')]
                data={'teams':[h.team_id,a.team_id],'names':[h.team_name,a.team_name],'projected_points':projections,
                      'recorded_at':datetime.now(timezone.utc).isoformat(),'kind':'Pregame ESPN projection, not an AI claim'}
                game=':'.join(map(str,sorted((h.team_id,a.team_id))))
                db.execute('INSERT OR IGNORE INTO analyst_forecasts VALUES (?,?,?,?)',(key,week,game,json.dumps(data)))
        rows=db.execute('SELECT week,data FROM analyst_forecasts WHERE scope=? AND week<=? ORDER BY week DESC LIMIT 5',(key,min(week,league.scoringPeriodId-1))).fetchall()
    by_id={t.team_id:t for t in league.teams}
    result=[]
    for w,raw in rows:
        data=json.loads(raw)
        scores=[]
        for tid in data['teams']:
            team=by_id.get(tid)
            outcomes=getattr(team,'outcomes',[])
            points=getattr(team,'scores',[])
            if w>len(outcomes) or outcomes[w-1] not in ('W','L','T') or w>len(points) or not finite(points[w-1]): break
            scores.append(points[w-1])
        if len(scores)==2:
            result.append({'week':w,**data,'actual_scores':scores})
    return result
