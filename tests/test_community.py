from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import pytest
from gamedaybot.espn import community as c, team_alerts as alerts, trade_followups as follow
from gamedaybot.espn.community_state import state, scope, reserve
from gamedaybot.chat.discord_format import TeamReport, build_payloads


def player(pid, slot='RB', points=10, projection=12, eligible=None, date=None):
    return NS(playerId=pid,name=f'Player {pid}',slot_position=slot,points=points,projected_points=projection,
              eligibleSlots=eligible or ['RB','RB/WR/TE'],proTeam='BUF',injuryStatus='QUESTIONABLE',
              game_date=date or datetime.now()+timedelta(days=2),game_played=0)


@pytest.fixture
def league(tmp_path,monkeypatch):
    monkeypatch.setenv('COMMUNITY_STATE_PATH',str(tmp_path/'community.db'))
    teams=[NS(team_id=i,team_name=f'Team {i}',team_abbrev=f'T{i}',standing=i,wins=2-i,losses=i-1,ties=0,
              schedule=[],scores=[100,90,80],outcomes=['W','W','U']) for i in (1,2)]
    box=NS(home_team=teams[0],away_team=teams[1],home_score=100,away_score=90,
           home_lineup=[player(1),player(3,'BE',30)],away_lineup=[player(2,points=5,projection=20)])
    l=NS(teams=teams,settings=NS(playoff_team_count=1,reg_season_count=3,division_map={},median_scoring=False,
                              matchup_periods={'1':[1],'2':[2],'3':[3]}),scoringPeriodId=3,league_id=123,year=2026,box=box)
    monkeypatch.setattr(c.espn,'fetch_box_scores',lambda league,week=None:[box])
    return l


def test_bench_award_requires_eligible_swap(league):
    text=c.awards(league,[league.box],2)
    assert 'would add 20.00' in text
    league.box.home_lineup[1].eligibleSlots=['QB']
    assert 'bench malpractice' not in c.awards(league,[league.box],2)


def test_awards_use_actual_loss_margin_and_projection(league):
    text=c.awards(league,[league.box],2)
    assert 'by 10.00' in text and '90.00' in text and '8.00-point projected deficit' in text


def test_watch_opponent_remaining_and_exact_margin(league):
    text=c.monday_watch(league,[league.box],{'BUF':{'state':'in'}})
    assert '10.01 MORE points than its opponent' in text
    assert 'Player 1' in text and 'Player 2' in text and 'Player 3' not in text
    assert c.monday_watch(league,[league.box],{'BUF':{'state':'post'}})==''


def test_playoff_bounds_and_unresolved_ties(league):
    league.teams[0].wins=3
    league.teams[1].losses=3
    text=c.playoff_picture(league)
    assert 'Clinched by record bound' in text and 'Eliminated by record bound' in text
    league.teams[0].wins=1
    league.teams[1].losses=1
    text=c.playoff_picture(league)
    assert 'Clinched by record bound' not in text and 'Eliminated by record bound' not in text


def test_no_clinches_for_division_or_median_rules(league):
    league.settings.division_map={1:'East',2:'West'}
    assert 'unconfirmed' in c.playoff_picture(league)
    league.settings.division_map={}
    league.settings.median_scoring=True
    assert 'unconfirmed' in c.playoff_picture(league)


def test_picks_update_persist_scope_and_lock(league):
    future={'BUF':{'start':datetime.now(timezone.utc).timestamp()+3600}}
    assert 'Saved: Team 1' in c.pickem(league,[league.box],10,'A',league.teams[0],future)
    assert 'Saved: Team 2' in c.pickem(league,[league.box],10,'A',league.teams[1],future)
    with state() as db:
        assert db.execute('SELECT COUNT(*),MAX(team) FROM picks').fetchone()==(1,2)
    locked={'BUF':{'start':0}}
    assert 'locked' in c.pickem(league,[league.box],10,'A',league.teams[0],locked)
    assert 'unavailable' in c.pickem(league,[league.box],10,'A',league.teams[0],{})
    with state() as db: assert db.execute('SELECT team FROM picks').fetchone()[0]==2


def test_pick_settlement_correct_tie_and_undecided(league):
    with state() as db:
        for week in (1,2):
            for user,tid in (('a',1),('b',2)):
                db.execute('INSERT INTO picks VALUES (?,?,?,?,?,?,NULL)',(scope(league),week,user,user,'1:2',tid))
    league.teams[0].outcomes=['W','U']
    c.settle_picks(league)
    with state() as db:
        assert db.execute('SELECT result FROM picks ORDER BY week,user').fetchall()==[(1.0,),(0.0,),(None,),(None,)]
    league.teams[0].outcomes[1]='T'
    league.box.home_score=90
    c.settle_picks(league)
    with state() as db:
        assert db.execute('SELECT result FROM picks WHERE week=2').fetchall()==[(.5,),(.5,)]


def test_alerts_opt_in_starters_window_and_dedupe(league,monkeypatch):
    now=datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr(alerts,'nfl_games',lambda l:{'BUF':{'state':'pre','start':now+3600}})
    assert alerts.pending_alerts(league,now)==[]
    alerts.subscribe(league,123,league.teams[0])
    batches=alerts.pending_alerts(league,now)
    assert len(batches)==1 and len(batches[0][1])==1
    assert 'Player 1' in batches[0][3] and 'Player 3' not in batches[0][3]
    assert 'https://www.espn.com/nfl/player/_/id/1' in batches[0][3]
    assert reserve(scope(league),batches[0][1][0])
    assert not reserve(scope(league),batches[0][1][0])
    assert alerts.pending_alerts(league,now)==[]
    league.box.home_lineup[0].injuryStatus='OUT'
    assert len(alerts.pending_alerts(league,now))==1
    alerts.subscribe(league,123,enabled=False)
    assert alerts.pending_alerts(league,now)==[]


def test_followup_requires_two_full_weeks_and_only_started_points(league,monkeypatch):
    now=datetime.now(timezone.utc)
    trade_date=(now-timedelta(days=25)).timestamp()*1000
    p=league.box.home_lineup[0]
    bench=league.box.home_lineup[1]
    for lineup in (league.box.home_lineup,league.box.away_lineup):
        for pl in lineup: pl.game_date=datetime.now()-timedelta(days=20)
    monkeypatch.setattr(follow,'completed_trades',lambda l,s:[{'id':'trade','date':trade_date,'actions':[
        (league.teams[0],'TRADE_RECEIVED',p),(league.teams[0],'TRADE_RECEIVED',bench),
        (league.teams[1],'TRADE_RECEIVED',league.box.away_lineup[0])]}])
    rows=follow.followups(league,now)
    assert len(rows)==1
    assert 'Team 1: 20.00 started points' in rows[0][1]
    assert 'Team 2: 10.00 started points' in rows[0][1]
    league.scoringPeriodId=2
    assert follow.followups(league,now)==[]


def test_new_reports_are_single_readable_messages(league,monkeypatch):
    monkeypatch.setattr(c,'nfl_games',lambda league:{'BUF':{'state':'pre','completed':False,'start':datetime.now(timezone.utc).timestamp()+3600}})
    reports=[c.preview(league,[league.box]),c.awards(league,[league.box],2),c.playoff_picture(league),
             c.monday_watch(league,[league.box],{'BUF':{'state':'pre'}}),c.pickem(league,[league.box],10,'A')]
    for text in reports:
        payloads=list(build_payloads(TeamReport(text,league.teams)))
        assert len(payloads)==1
        assert len(payloads[0]['embeds'])==1
        assert '```' not in payloads[0]['embeds'][0]['description']
        assert '?' not in payloads[0]['embeds'][0]['title']

def test_alert_gateway_uses_png_and_preserves_inline_source(league,monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from gamedaybot.chat.interactions import LeagueClient
    from gamedaybot.espn import command_reports
    from gamedaybot.chat import discord_images
    monkeypatch.setattr(command_reports,'get_env_vars',lambda **kw:{'swid':'{1}','espn_s2':'1','league_id':123,'year':2026})
    monkeypatch.setattr(command_reports,'League',lambda **kw:league)
    monkeypatch.setattr(alerts,'pending_alerts',lambda l:[('123',['test-alert'],'Team 1','[ESPN](https://www.espn.com/nfl/player/_/id/1)','https://g.espncdn.com/logo.png')])
    monkeypatch.setattr(discord_images,'fetch_logo',lambda u:b'png-bytes')
    async def run():
        client=LeagueClient(123)
        recipient=NS(send=AsyncMock())
        client.fetch_user=AsyncMock(return_value=recipient)
        await client.alert_loop.coro(client)
        kwargs=recipient.send.call_args.kwargs
        assert kwargs['embed'].thumbnail.url=='attachment://team.png'
        assert len(kwargs['files'])==1
        assert '[ESPN](' in kwargs['embed'].description
        await client.alert_loop.coro(client)
        assert recipient.send.await_count==1
        await client.close()
    asyncio.run(run())


def test_unsubscribe_works_without_espn(league,monkeypatch):
    from gamedaybot.espn import command_reports
    alerts.subscribe(league,123,league.teams[0])
    monkeypatch.setattr(command_reports,'get_env_vars',lambda **kw:{'league_id':123,'year':2026})
    monkeypatch.setattr(command_reports,'League',lambda **kw:pytest.fail('Unsubscribe must not fetch ESPN'))
    assert 'disabled' in command_reports.command_report('alerts',user=123,enabled=False)
    with state() as db: assert db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0]==0


def test_multiweek_picks_are_not_mistaken_for_weekly_games(league):
    league.settings.matchup_periods={'1':[1],'2':[2,3]}
    future={'BUF':{'start':datetime.now(timezone.utc).timestamp()+3600}}
    assert 'multi-week' in c.pickem(league,[league.box],10,'A',league.teams[0],future)
    assert c.final_outcome(league,league.teams[0],2) is None


def test_schedule_mismatch_fails_closed(league,monkeypatch):
    response=NS(raise_for_status=lambda:None,json=lambda:{'season':{'year':2025,'type':2},'week':{'number':3},'events':[]})
    monkeypatch.setattr(c.requests,'get',lambda *a,**kw:response)
    with pytest.raises(ValueError,match='does not match'): c.nfl_games(league)

def test_rivalry_uses_live_projection_and_skips_completed_matchups(league,monkeypatch):
    league.box.home_projected=110
    league.box.away_projected=112
    monkeypatch.setattr(c,'nfl_games',lambda league:{'BUF':{'state':'pre','completed':False}})
    text=c.preview(league,[league.box])
    assert 'Projected finish: 110.00 - 112.00' in text and 'Team 2 by 2.00' in text
    monkeypatch.setattr(c,'nfl_games',lambda league:{'BUF':{'state':'post','completed':True}})
    assert 'No unfinished matchup' in c.preview(league,[league.box])
