"""Opt-in pre-kickoff injury notices, batched into one private message per user."""
from datetime import datetime,timezone
from gamedaybot.chat.discord_format import escape, logo_url
from gamedaybot.espn.community import starters,nfl_games
from gamedaybot.espn.community_state import state,scope
from gamedaybot.espn import functionality as espn

RISK={'QUESTIONABLE','DOUBTFUL','OUT','INJURY_RESERVE','SUSPENSION','SUSPENDED','IR'}


def subscribe(league,user,team=None,enabled=True):
    with state() as db:
        if not enabled:
            db.execute('DELETE FROM subscriptions WHERE scope=? AND user=?',(scope(league),str(user)))
            return 'Team alerts\nPrivate team alerts disabled.'
        db.execute('INSERT INTO subscriptions VALUES (?,?,?) ON CONFLICT(scope,user) DO UPDATE SET team=excluded.team',
                   (scope(league),str(user),team.team_id))
    return f'Team alerts\nSubscribed to {team.team_name}. Starting-player injury alerts arrive by DM within two hours before kickoff. Allow server-member DMs to receive them. Use /alerts enabled:false to unsubscribe.'


def pending_alerts(league,now=None):
    now=now or datetime.now(timezone.utc).timestamp()
    with state() as db:
        subscribers=db.execute('SELECT user,team FROM subscriptions WHERE scope=?',(scope(league),)).fetchall()
        seen={r[0] for r in db.execute('SELECT key FROM notices WHERE scope=?',(scope(league),))}
    if not subscribers:
        return []
    games=nfl_games(league)
    boxes=espn.fetch_box_scores(league)
    by_team={getattr(b,s+'_team').team_id: (getattr(b,s+'_team'),getattr(b,s+'_lineup'))
             for b in boxes for s in ('home','away') if getattr(getattr(b,s+'_team',None),'team_id',None)}
    batches=[]
    for user,tid in subscribers:
        if tid not in by_team: continue
        team,lineup=by_team[tid]
        rows=[]
        keys=[]
        for p in starters(lineup):
            game=games.get(p.proTeam)
            status=str(getattr(p,'injuryStatus','UNKNOWN')).upper()
            if not game or game['state']!='pre' or not 0<game['start']-now<=7200 or status not in RISK:
                continue
            key=f'alert:{user}:{tid}:{league.scoringPeriodId}:{p.playerId}:{status}'
            if key in seen: continue
            keys.append(key)
            rows.append(f'**{escape(p.name)}** — {status.replace("_"," ")} · kickoff <t:{int(game["start"])}:t>\n[ESPN player status](https://www.espn.com/nfl/player/_/id/{int(p.playerId)})')
        if rows:
            batches.append((user,keys,f'{team.team_name} · lineup alert','\n\n'.join(rows)+'\n\nESPN status checked just now. Questionable is not confirmed inactive; recheck your lineup before kickoff.',logo_url(team)))
    return batches
