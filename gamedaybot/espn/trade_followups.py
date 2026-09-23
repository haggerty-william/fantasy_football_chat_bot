"""Trade report cards revisited after two full completed scoring weeks."""
from datetime import datetime, timezone, timedelta
from gamedaybot.espn import functionality as espn
from gamedaybot.espn.trades import completed_trades
from gamedaybot.espn.community import valid_boxes, starters, final_outcome


def followups(league, now=None):
    now=now or datetime.now(timezone.utc)
    trades=completed_trades(league,int((now-timedelta(days=42)).timestamp()*1000))
    cache={}
    reports=[]
    for trade in trades:
        if now.timestamp()*1000-trade['date'] < 14*86400000:
            continue
        weeks=[]
        for week in range(max(1,league.scoringPeriodId-6),league.scoringPeriodId):
            if week not in cache:
                cache[week]=valid_boxes(espn.fetch_box_scores(league,week=week))
            boxes=cache[week]
            dates=[p.game_date.timestamp()*1000 for b in boxes for s in ('home','away') for p in getattr(b,s+'_lineup') if getattr(p,'game_date',None)]
            if dates and min(dates)>trade['date'] and all(
                    final_outcome(league, b.home_team, week) in ('W','L','T') for b in boxes):
                weeks.append(week)
        if len(weeks)<2:
            continue
        weeks=weeks[:2]
        received={}
        for team,action,player,*_ in trade['actions']:
            if action=='TRADE_RECEIVED':
                received.setdefault(team.team_id,(team,{}))[1][player.playerId]=player.name
        if len(received)<2:
            continue
        lines=['Trade follow-up',f'Completed {datetime.fromtimestamp(trade["date"]/1000,timezone.utc):%b %d} | Full weeks {weeks[0]} and {weeks[1]}']
        totals=[]
        for tid,(team,players) in received.items():
            started=0.0
            observed=set()
            for week in weeks:
                for box in cache[week]:
                    for side in ('home','away'):
                        if getattr(box,side+'_team').team_id!=tid:
                            continue
                        for p in getattr(box,side+'_lineup'):
                            if p.playerId in players:
                                observed.add((week,p.playerId))
                                if p in starters(getattr(box,side+'_lineup')):
                                    started+=p.points
            lines.append(f'{team.team_name}: {started:.2f} started points from {", ".join(players.values())}.')
            lines.append(f'Roster observations: {len(observed)}/{len(players)*len(weeks)} player-weeks. Missing players are not assumed to have scored zero.')
            totals.append((started,team.team_name))
        totals.sort(reverse=True)
        if totals[0][0]>totals[1][0]:
            lines.append(f'Starting-lineup return leader: {totals[0][1]} by {totals[0][0]-totals[1][0]:.2f} points. The receipts have arrived.')
        else:
            lines.append('Starting-lineup returns are tied. Put the victory parade on hold.')
        lines.append('Observed contributions only; excludes bench production, replacement value, and later trades. This is a follow-up, not a new trade announcement.')
        reports.append((trade['id']+':two-weeks','\n'.join(lines)))
    return reports
