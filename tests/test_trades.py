from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from espn_api.football.activity import Activity

from gamedaybot.espn.trades import get_trade_report


DAY = date(2026, 9, 20)


def trade(when='2026-09-20T16:00:00-04:00', player_id=10):
    """Exercise the installed ESPN adapter with raw trade messages."""
    teams = {
        1: SimpleNamespace(team_name='Maple Street', roster=[]),
        2: SimpleNamespace(team_name='Oak Street', roster=[]),
    }
    return Activity({
        'date': int(datetime.fromisoformat(when).timestamp() * 1000),
        'messages': [
            {'messageTypeId': 244, 'from': 1, 'to': 2, 'targetId': player_id},
            {'messageTypeId': 244, 'from': 2, 'to': 1, 'targetId': 99},
        ],
    }, {}, teams.get, lambda playerId: SimpleNamespace(name=f'Player {playerId}'))


def report(activities, **kwargs):
    league = SimpleNamespace(recent_activity=Mock(return_value=activities))
    return get_trade_report(league, report_date=DAY, **kwargs)


def test_reports_both_sides_of_completed_trade():
    result = report([trade()])
    assert 'Trade Report 2026-09-20' in result
    assert 'Maple Street sent Player 10' in result
    assert 'Oak Street received Player 10' in result
    assert 'Oak Street sent Player 99' in result
    assert 'Maple Street received Player 99' in result


def test_full_local_day_excludes_adjacent_days():
    result = report([
        trade('2026-09-21T00:00:00-04:00', 11),
        trade('2026-09-20T23:59:59-04:00', 12),
        trade('2026-09-20T00:00:00-04:00', 13),
        trade('2026-09-19T23:59:59-04:00', 14),
    ])
    assert 'Player 11' not in result
    assert 'Player 14' not in result
    assert result.index('sent Player 13') < result.index('sent Player 12')


def test_uses_configured_timezone_not_host_timezone():
    activity = trade('2026-09-21T02:30:00+00:00')
    assert report([activity], timezone='America/New_York')
    assert report([activity], timezone='UTC') == ''


def test_paginates_past_default_activity_limit():
    league = SimpleNamespace(recent_activity=Mock(side_effect=[
        [trade(player_id=i) for i in range(25)], [trade(player_id=1000)],
    ]))
    result = get_trade_report(league, report_date=DAY)
    assert 'Player 1000' in result
    assert result.count('Trade completed') == 26
    assert league.recent_activity.call_args_list[1].kwargs == {
        'size': 25, 'msg_type': 'TRADED', 'offset': 25}


def test_empty_day_is_silent():
    assert report([]) == ''
    assert report([trade('2026-09-19T16:00:00-04:00')]) == ''


def test_duplicate_activity_is_rendered_once():
    assert report([trade(), trade()]).count('Trade completed') == 1


def test_non_trade_actions_are_not_announced():
    activity = trade()
    activity.actions = [(None, 'WAIVER ADDED', 123, 0)]
    assert report([activity]) == ''


def test_unresolved_player_and_team_do_not_crash():
    activity = trade()
    activity.actions = [(None, 'TRADE_SENT', 123, 0)]
    assert 'Unknown team sent Player #123' in report([activity])


def test_api_failure_is_not_treated_as_a_quiet_day():
    league = SimpleNamespace(recent_activity=Mock(side_effect=RuntimeError('API unavailable')))
    with pytest.raises(RuntimeError, match='API unavailable'):
        get_trade_report(league, report_date=DAY)


def test_large_report_fits_discord_messages(monkeypatch):
    from gamedaybot.espn.env_vars import get_env_vars
    from gamedaybot.utils.util import str_limit_check

    monkeypatch.setenv('DISCORD_WEBHOOK_URL', 'unused')
    monkeypatch.setenv('LEAGUE_ID', '123')
    monkeypatch.delenv('BOT_ID', raising=False)
    league = SimpleNamespace(recent_activity=Mock(side_effect=[
        [trade(player_id=i) for i in range(25)], [],
    ]))
    text = get_trade_report(league, report_date=DAY)
    messages = str_limit_check(text, get_env_vars()['str_limit'])
    assert len(messages) > 1
    assert all(len(f'```{message}```') <= 2000 for message in messages)
    assert sum(message.count('Trade completed') for message in messages) == 25


@pytest.mark.parametrize('long_report', [False, True])
def test_dispatch_sends_trade_report(monkeypatch, long_report):
    import gamedaybot.espn.espn_bot as bot
    monkeypatch.delenv('AI_MODEL', raising=False)

    league = SimpleNamespace(scoringPeriodId=2, firstScoringPeriod=1, finalScoringPeriod=18)
    monkeypatch.setattr(bot, 'get_env_vars', lambda: {
        'str_limit': 1900, 'league_id': 123, 'discord_webhook_url': 'unused',
        'my_timezone': 'America/New_York',
    })
    monkeypatch.setattr(bot, 'League', Mock(return_value=league))
    text = 'Trade Report\nOak Street received Player 10'
    if long_report:
        text += '\nOak Street received Player 20' * 100
    generate = Mock(return_value=text)
    monkeypatch.setattr(bot, 'get_trade_report', generate)
    destinations = []
    for name in ('Discord', 'Slack', 'GroupMe'):
        destination = Mock()
        monkeypatch.setattr(bot, name, Mock(return_value=destination))
        destinations.append(destination)
    bot.espn_bot('get_trade_report')
    generate.assert_called_once_with(league, timezone='America/New_York')
    destinations[0].send_message.assert_called_once_with(text)
    for destination in destinations[1:]:
        assert destination.send_message.called
        assert all(len(call.args[0]) <= 1900 for call in destination.send_message.call_args_list)
