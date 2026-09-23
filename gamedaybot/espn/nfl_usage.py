"""Bounded public nflverse usage feeds, joined by ESPN -> GSIS/PFR IDs."""
import csv
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
import requests
from gamedaybot.espn.community_state import state

ROOT = 'https://github.com/nflverse/nflverse-data/releases/download/'
_cache = {}
_lock = Lock()
COLUMNS = {
    'players': ('espn_id','gsis_id','pfr_id'),
    'stats_player': ('player_id','season','week','season_type','targets','carries','receptions','target_share',
                     'passing_yards','passing_tds','passing_interceptions','rushing_yards','rushing_tds',
                     'receiving_yards','receiving_tds','fumbles_lost_total'),
    'snap_counts': ('pfr_player_id','season','week','game_type','offense_snaps','offense_pct'),
}


def numeric(value):
    try:
        n=float(value)
        return round(n,3) if math.isfinite(n) else None
    except (TypeError,ValueError):
        return None


def dataset(kind, year):
    name = {'players':'players.csv','stats_player':f'stats_player_week_{year}.csv',
            'snap_counts':f'snap_counts_{year}.csv'}[kind]
    key=kind+':'+name
    ttl=86400 if kind=='players' else 3600
    now=time.time()
    with _lock:
        cached=_cache.get(key)
        if cached and now-cached[0]<ttl:
            return cached[1]
    with state() as db:
        db.execute('CREATE TABLE IF NOT EXISTS research_cache(key TEXT PRIMARY KEY,fetched REAL,data TEXT)')
        row=db.execute('SELECT fetched,data FROM research_cache WHERE key=?',(key,)).fetchone()
    if row and now-row[0]<ttl:
        result=json.loads(row[1])
        with _lock: _cache[key]=(row[0],result)
        return result
    url=ROOT+kind+'/'+name
    started=time.monotonic()
    with requests.get(url,timeout=(3,8),stream=True) as r:
        r.raise_for_status()
        body=bytearray()
        for chunk in r.iter_content(65536):
            body.extend(chunk)
            if len(body)>32_000_000 or time.monotonic()-started>12:
                raise ValueError('Usage feed exceeds limits')
    reader=csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    if not set(COLUMNS[kind]).issubset(reader.fieldnames or []):
        raise ValueError('Usage feed schema changed')
    rows=[]
    for row in reader:
        if len(rows)>=100000: raise ValueError('Usage row limit')
        if kind!='players' and (numeric(row['season'])!=year or row.get('season_type',row.get('game_type'))!='REG'):
            continue
        rows.append({k:row[k] for k in COLUMNS[kind]})
    result={'rows':rows,'source':url,'fetched_at':datetime.now(timezone.utc).isoformat()}
    with state() as db:
        db.execute('INSERT OR REPLACE INTO research_cache VALUES (?,?,?)',(key,now,json.dumps(result)))
    with _lock: _cache[key]=(now,result)
    return result


def usage_context(year, player_ids, through_week, completed_weeks=None):
    """Completed weeks only; old weeks remain explicitly dated, never live stats."""
    if not isinstance(year,int) or not isinstance(through_week,int) or through_week<1:
        return {}, {'status':'No completed week available'}
    feeds={}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={k:pool.submit(dataset,k,year) for k in COLUMNS}
        for kind,job in jobs.items():
            try: feeds[kind]=job.result()
            except (requests.RequestException,ValueError,KeyError,UnicodeError,OSError): pass
    if 'players' not in feeds:
        return {}, {'status':'Unavailable: player ID mapping missing'}
    ids={}
    ambiguous=set()
    for row in feeds['players']['rows']:
        pid=row['espn_id'].removesuffix('.0')
        if pid in ids and ids[pid]!=row:
            ambiguous.add(pid)
        ids[pid]=row
    result={}
    stats={}
    snaps={}
    for row in feeds.get('stats_player',{}).get('rows',[]):
        w=numeric(row['week'])
        if w is not None and max(1,through_week-3)<=w<=through_week:
            stats.setdefault(row['player_id'],[]).append(row)
    for row in feeds.get('snap_counts',{}).get('rows',[]):
        w=numeric(row['week'])
        if w is not None and max(1,through_week-3)<=w<=through_week:
            snaps.setdefault(row['pfr_player_id'],[]).append(row)
    for player_id in player_ids:
        pid=str(player_id)
        if pid not in ids or pid in ambiguous: continue
        mapping=ids[pid]
        cutoff=(completed_weeks or {}).get(pid,through_week)
        by_week={}
        for row in stats.get(mapping['gsis_id'],[]):
            w=int(row['week'])
            if not max(1,cutoff-2)<=w<=cutoff: continue
            by_week[w]={k:numeric(row[k]) for k in COLUMNS['stats_player'][4:] if numeric(row[k]) is not None}
            if 'target_share' in by_week[w]:
                by_week[w]['target_share_pct']=round(by_week[w].pop('target_share')*100,1)
        for row in snaps.get(mapping['pfr_id'],[]):
            w=int(row['week'])
            if not max(1,cutoff-2)<=w<=cutoff: continue
            item=by_week.setdefault(w,{})
            if numeric(row['offense_snaps']) is not None: item['offense_snaps']=numeric(row['offense_snaps'])
            if numeric(row['offense_pct']) is not None: item['offense_snap_pct']=round(numeric(row['offense_pct'])*100,1)
        if by_week:
            ordered=[{'week':w,**v} for w,v in sorted(by_week.items())]
            result[pid]={'weeks':ordered,'latest_week':max(by_week),'through_week_requested':cutoff}
            if len(ordered)>=2 and ordered[-1]['week']==ordered[-2]['week']+1:
                result[pid]['change_from_previous_week']={k:round(ordered[-1][k]-ordered[-2][k],2)
                    for k in ('targets','carries','offense_snap_pct') if k in ordered[-1] and k in ordered[-2]}
    return result,{'status':'Completed-game usage; missing rows are unknown, not zero',
                   'feeds':{k:{'source':v['source'],'fetched_at':v['fetched_at']} for k,v in feeds.items()},
                   'missing_feeds':sorted(set(COLUMNS)-set(feeds))}
