from types import SimpleNamespace as Obj
from unittest.mock import Mock
import sqlite3
import pytest

from gamedaybot.espn.trades import completed_trades
from gamedaybot.espn import trade_notifications as notifications
from gamedaybot.espn.trade_notifications import TradeLedger


def activity(date=2000):
    a=Obj(team_id=1,team_name='Oak')
    b=Obj(team_id=2,team_name='Maple')
    p=Obj(playerId=11,name='Player One')
    return Obj(date=date,actions=[(a,'TRADE_SENT',p,0),(b,'TRADE_RECEIVED',p,0)])


def test_identity_survives_team_names_and_action_order():
    a=activity()
    league=Obj(recent_activity=Mock(return_value=[a,a]))
    first=completed_trades(league,1000)
    assert len(first)==1
    a.actions.reverse()
    a.actions[0][0].team_name='Renamed'
    assert completed_trades(league,1000)[0]['id']==first[0]['id']


def test_scan_cutoff_pagination_and_error():
    league=Obj(recent_activity=Mock(side_effect=[[activity(2000+i) for i in range(25)],[activity(999),activity(3000)]]))
    result=completed_trades(league,1000)
    assert len(result)==26
    assert league.recent_activity.call_args.kwargs['offset']==25
    league.recent_activity=Mock(side_effect=RuntimeError('ESPN unavailable'))
    with pytest.raises(RuntimeError): completed_trades(league,1000)


def test_ledger_survives_restart_and_claim_is_exclusive(tmp_path):
    path=tmp_path/'ledger.sqlite'
    first=TradeLedger(path)
    assert first.start('league',1000)==1000
    assert first.claim('league','trade')
    second=TradeLedger(path)
    assert second.start('league',3000)==1000
    assert not second.claim('league','trade')
    first.finish('league','trade','sent')
    first.close();second.close()
    third=TradeLedger(path)
    assert third.known('league','trade')
    assert third.claim('other-league','trade')
    third.close()


@pytest.mark.parametrize('failure',[False,True])
def test_notification_only_attempted_once_across_polls(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('TRADE_STATE_PATH',str(tmp_path/'ledger.sqlite'))
    # Watch starts at 1000ms; trade happens later at 2000ms.
    monkeypatch.setattr(notifications.time,'time',lambda:1)
    league=Obj(teams=[],scoringPeriodId=2,recent_activity=Mock(return_value=[activity()]))
    monkeypatch.setattr(notifications,'generate_analysis',Mock(return_value='AI Analysis\nA lopsided deal.'))
    discord=Mock()
    if failure: discord.send_message.side_effect=TimeoutError('ambiguous receipt')
    data={'league_id':123,'year':2026,'discord_webhook_url':'https://discord.test/hook','my_timezone':'UTC'}
    notifications.poll_trades(league,data,discord)
    notifications.poll_trades(league,data,discord)
    assert discord.send_message.call_count==1
    assert notifications.generate_analysis.call_count==1
    assert 'trade_actions' in notifications.generate_analysis.call_args.kwargs
    with sqlite3.connect(tmp_path/'ledger.sqlite') as db:
        assert db.execute('SELECT status FROM deliveries').fetchone()[0]==('uncertain' if failure else 'sent')


def test_first_run_does_not_reannounce_old_trades(tmp_path,monkeypatch):
    monkeypatch.setenv('TRADE_STATE_PATH',str(tmp_path/'ledger.sqlite'))
    monkeypatch.setattr(notifications.time,'time',lambda:10)
    league=Obj(recent_activity=Mock(return_value=[activity()]))
    discord=Mock()
    notifications.poll_trades(league,{'league_id':123,'discord_webhook_url':'configured','my_timezone':'UTC'},discord)
    discord.send_message.assert_not_called()


def test_trades_command_is_read_only_and_supports_empty_history(monkeypatch):
    from gamedaybot.espn import command_reports as reports
    monkeypatch.setattr(reports,'get_env_vars',lambda **k:{'league_id':123,'year':2026,'swid':'{1}','espn_s2':'1','my_timezone':'UTC'})
    monkeypatch.setattr(reports,'League',lambda **k:Obj(teams=[],scoringPeriodId=2))
    scanner=Mock(return_value=[])
    monkeypatch.setattr(reports,'completed_trades',scanner)
    assert 'No completed trades' in reports.command_report('trades')
    with pytest.raises(reports.ReportInputError): reports.command_report('trades',days=31)
    scanner.return_value=[{'id':'one','date':2000,'actions':activity().actions}]
    analysis=Mock(return_value='AI Analysis\nOak wins this one.')
    monkeypatch.setattr(reports,'generate_analysis',analysis)
    assert 'Oak wins' in reports.command_report('trades')
    assert analysis.call_args.kwargs['trade_actions']
