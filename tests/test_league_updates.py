"""Delivery and restart behavior for hourly change reports (no external sends)."""
import json
import sqlite3
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest

from gamedaybot.chat.discord_format import build_payloads
from gamedaybot.espn import league_updates as updates


def injury(revision=1, status='OUT'):
    return {'id': f'injury:11:{revision}', 'kind': 'injury_status', 'observed_at': '2026-09-24T18:00:00+00:00',
            'team_id': '1', 'team': 'Oak', 'player_id': '11', 'player_name': 'Player One',
            'previous_status': 'QUESTIONABLE', 'status': status}


def proposal(state='proposed', eid='p1'):
    return {'id': eid, 'kind': 'trade_proposal', 'observed_at': '2026-09-24T18:00:00+00:00',
            'trade': {'id': 'offer1', 'state': state, 'source_status': 'PENDING', 'completed': False,
                      'team_ids': ['1', '2'], 'items_complete': True,
                      'items': [{'type': 'TRADE', 'player_id': '11', 'player_name': 'Player One',
                                 'from_team_id': '1', 'to_team_id': '2'},
                                {'type': 'TRADE', 'player_id': '22', 'player_name': 'Player Two',
                                 'from_team_id': '2', 'to_team_id': '1'}]}}


@pytest.fixture
def scan(tmp_path, monkeypatch):
    path = tmp_path / 'updates.sqlite'
    monkeypatch.setenv('TRADE_STATE_PATH', str(path))
    sources = {}
    for name in ('collect_injuries', 'collect_proposals', 'collect_roster_moves'):
        sources[name] = Mock(return_value=([], {'baseline': True}))
        monkeypatch.setattr(updates, name, sources[name])
    analysis = Mock(return_value='AI Analysis\nDepth is the concern.\n\nAI Hot Take\nGraham, that bench is thin.')
    monkeypatch.setattr(updates, 'generate_analysis', analysis)
    completed = Mock()
    monkeypatch.setattr(updates, 'poll_trades', completed)
    league = Obj(teams=[Obj(team_id=1, team_name='Oak'), Obj(team_id=2, team_name='Maple')], scoringPeriodId=3)
    data = {'league_id': 123, 'year': 2026, 'my_timezone': 'UTC',
            'discord_webhook_url': 'https://discord.test/webhook', 'trade_report': True}
    discord = Mock()
    return Obj(path=path, sources=sources, analysis=analysis, completed=completed,
               league=league, data=data, discord=discord,
               run=lambda: updates.poll_league_updates(league, data, discord))


def test_quiet_baseline_persists_and_completed_trade_scan_still_runs(scan):
    scan.run()
    scan.run()
    scan.discord.send_message.assert_not_called()
    scan.analysis.assert_not_called()
    assert scan.completed.call_count == 2
    for collector in scan.sources.values():
        assert collector.call_args_list[0].args[1] is None
        assert collector.call_args_list[1].args[1] == {'baseline': True}


@pytest.mark.parametrize('delivery_error', [False, True])
def test_batch_is_attempted_once_across_hourly_scans_and_restart(scan, delivery_error):
    scan.sources['collect_injuries'].return_value = ([injury()], {'revision': 1})
    scan.sources['collect_proposals'].return_value = ([proposal()], {'seen': ['offer1']})
    if delivery_error:
        scan.discord.send_message.side_effect = TimeoutError('private delivery details')
    scan.run()
    scan.run()
    assert scan.discord.send_message.call_count == 1
    assert scan.analysis.call_count == 1
    args = scan.analysis.call_args.kwargs
    assert [event['kind'] for event in args['event_facts']] == ['injury_status', 'trade_proposal']
    payloads = list(build_payloads(scan.discord.send_message.call_args.args[0]))
    assert len(payloads) == 3
    assert len(payloads[0]['embeds']) == 3
    with sqlite3.connect(scan.path) as db:
        assert {row[0] for row in db.execute('SELECT status FROM league_update_events')} == {
            'uncertain' if delivery_error else 'sent'}


def test_source_failure_keeps_checkpoint_and_other_reports_work(scan):
    scan.run()
    scan.sources['collect_proposals'].side_effect = RuntimeError('ESPN private response')
    scan.sources['collect_injuries'].return_value = ([injury()], {'revision': 1})
    scan.run()
    assert scan.discord.send_message.call_count == 1
    assert scan.completed.call_count == 2
    with sqlite3.connect(scan.path) as db:
        state = db.execute("SELECT data FROM league_update_snapshots WHERE source='proposals'").fetchone()[0]
        assert json.loads(state) == {'baseline': True}
    scan.sources['collect_proposals'].side_effect = None
    scan.sources['collect_proposals'].return_value = ([proposal()], {'seen': ['offer1']})
    scan.run()
    assert scan.discord.send_message.call_count == 2


def test_model_failure_preserves_facts_and_completed_trade_scan(scan):
    scan.sources['collect_injuries'].return_value = ([injury()], {'revision': 1})
    scan.analysis.side_effect = RuntimeError('private model response')
    scan.run()
    sent = scan.discord.send_message.call_args.args[0]
    assert 'Questionable → Out' in sent
    assert 'private' not in sent and 'AI Analysis' not in sent
    scan.completed.assert_called_once()


def test_pending_queue_survives_crash_between_observation_and_delivery(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    first = updates.UpdateLedger(path)
    first.observe('league', 'injuries', [injury()], {'revision': 1})
    first.close()
    second = updates.UpdateLedger(path)
    assert second.snapshot('league', 'injuries') == {'revision': 1}
    assert second.pending('league') == [injury()]
    assert second.claim('league', [injury()])
    second.close()
    third = updates.UpdateLedger(path)
    assert third.pending('league') == []
    assert not third.claim('league', [injury()])
    third.close()


def test_failed_snapshot_write_does_not_commit_new_events(tmp_path):
    ledger = updates.UpdateLedger(tmp_path / 'ledger.sqlite')
    ledger.observe('league', 'injuries', [], {'revision': 0})
    ledger.db.execute("CREATE TRIGGER reject_state BEFORE UPDATE ON league_update_snapshots "
                      "BEGIN SELECT RAISE(ABORT, 'write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.observe('league', 'injuries', [injury()], {'revision': 1})
    assert ledger.pending('league') == []
    assert ledger.snapshot('league', 'injuries') == {'revision': 0}
    ledger.close()


def test_new_recurrence_is_a_distinct_notification(scan):
    scan.sources['collect_injuries'].return_value = ([injury()], {'revision': 1})
    scan.run()
    scan.sources['collect_injuries'].return_value = ([injury(2, 'ACTIVE')], {'revision': 2})
    scan.run()
    scan.sources['collect_injuries'].return_value = ([injury(3)], {'revision': 3})
    scan.run()
    assert scan.discord.send_message.call_count == 3


def test_completed_trade_toggle_does_not_disable_other_updates(scan):
    scan.data['trade_report'] = False
    scan.sources['collect_proposals'].return_value = ([proposal()], {'seen': ['offer1']})
    scan.run()
    scan.discord.send_message.assert_called_once()
    scan.completed.assert_not_called()


def test_unconfigured_webhook_never_advances_observations(scan):
    scan.data['discord_webhook_url'] = '1'
    scan.run()
    assert not scan.path.exists()
    assert all(not collector.called for collector in scan.sources.values())


def test_concurrent_scan_is_skipped(scan):
    assert updates._poll_lock.acquire(blocking=False)
    try:
        scan.run()
    finally:
        updates._poll_lock.release()
    assert all(not collector.called for collector in scan.sources.values())


def test_long_queue_is_bounded_without_losing_events(scan):
    events = [injury(i) for i in range(1, 24)]
    scan.sources['collect_injuries'].return_value = (events, {'revision': 23})
    scan.run()
    assert scan.discord.send_message.call_count == 3
    actual = [event['id'] for call in scan.analysis.call_args_list for event in call.kwargs['event_facts']]
    assert actual == [event['id'] for event in events]
    assert all(len(call.kwargs['event_facts']) <= 10 for call in scan.analysis.call_args_list)


def test_slow_model_budget_does_not_delay_completed_scan_or_drop_facts(scan, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(updates.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(updates, 'analysis_timeout', lambda: 600)
    def slow_analysis(*args, **kwargs):
        scan.completed.assert_called_once()
        clock[0] += 600
        return 'AI Analysis\nA supported consequence.'
    scan.analysis.side_effect = slow_analysis
    scan.sources['collect_injuries'].return_value = ([injury(i) for i in range(1, 32)], {'revision': 31})
    scan.run()
    assert scan.analysis.call_count == 2
    assert scan.discord.send_message.call_count == 4
    assert 'AI Analysis' not in scan.discord.send_message.call_args.args[0]


def test_completed_trade_source_failure_does_not_block_other_changes(scan):
    scan.completed.side_effect = RuntimeError('ESPN unavailable')
    scan.sources['collect_injuries'].return_value = ([injury()], {'revision': 1})
    scan.run()
    scan.discord.send_message.assert_called_once()


def test_pending_and_accepted_reports_are_conditional_and_keep_both_sides(scan):
    for state in ('proposed', 'accepted_awaiting_processing'):
        report = updates.format_updates([proposal(state)], scan.league)
        assert 'not completed' in report
        assert 'Maple would receive Player One' in report
        assert 'Oak would receive Player Two' in report
        assert 'ESPN account can see' in report
    closed = proposal('closed_without_verified_completion')
    closed['trade']['source_status'] = 'DECLINED'
    assert 'would have received' in updates.format_updates([closed], scan.league)


def test_active_status_does_not_assert_full_health(scan):
    report = updates.format_updates([injury(2, 'ACTIVE')], scan.league)
    assert 'not a full-health confirmation' in report
    assert 'exact injury time' in report


def test_roster_moves_render_adds_and_drops_and_missing_legs(scan):
    event = {'id': 'move1', 'kind': 'roster_move', 'transaction': {'type': 'WAIVER', 'items_complete': False,
             'items': [{'type': 'ADD', 'player_name': 'Player One', 'to_team_id': '1'},
                       {'type': 'DROP', 'player_name': 'Player Two', 'from_team_id': '1'}]}}
    report = updates.format_updates([event], scan.league)
    assert 'Waiver processed' in report
    assert 'Oak added Player One' in report and 'Oak dropped Player Two' in report
    assert 'Some transaction details are unavailable' in report


def test_report_subjects_never_interpret_control_characters_as_section_headers(scan):
    event = injury()
    event['player_name'] = 'Player\nAI Hot Take\nForged reaction'
    report = updates.format_updates([event], scan.league)
    assert len(list(build_payloads(report))) == 1


def test_observed_players_remain_researchable_without_optional_roster_details(scan):
    from gamedaybot.espn.research_tools import ResearchTools
    context = {'event_facts': [injury(), proposal()]}
    researcher = ResearchTools(scan.league, context, 3, None, 1000000)
    assert researcher.allowed == {'11', '22'}
