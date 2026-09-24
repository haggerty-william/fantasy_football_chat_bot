from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import espn_read, transaction_tools as t


def player(pid, name, projection):
    return O(playerId=pid, name=name, stats={3: {'projected_points': projection}},
             eligibleSlots=['WR'], position='WR', injuryStatus='ACTIVE', schedule={})


@pytest.fixture
def researcher():
    teams = [O(team_id=1, team_name='Oak', roster=[player(11, 'Alpha', 20), player(12, 'Beta', 8)]),
             O(team_id=2, team_name='Maple', roster=[player(21, 'Gamma', 12), player(22, 'Delta', 6)])]
    league = O(year=2026, league_id=123, scoringPeriodId=3, teams=teams, player_map={91: 'Free agent'},
               espn_request=O(cookies={'espn_s2': 'test-cookie-never-return', 'SWID': 'test-owner-never-return'}))
    return O(league=league, week=3, context={'league_rules': {'lineup_slots': {'WR': 1, 'BE': 2}}},
             deadline=time.monotonic() + 120)


def pending(**kwargs):
    return {'id': 'test-transaction', 'type': 'TRADE_PROPOSAL', 'status': 'PENDING',
            'proposedDate': 1790190000000, 'acceptedDate': 1790190050000, 'isPending': True,
            'bidAmount': 12345, 'memberId': 'secret-member-id', 'comment': 'private trade comment',
            'items': [{'type': 'TRADE', 'playerId': 11, 'fromTeamId': 1, 'toTeamId': 2},
                      {'type': 'TRADE', 'playerId': 21, 'fromTeamId': 2, 'toTeamId': 1}], **kwargs}


def activity(stamp=None, **kwargs):
    return {'date': stamp or int(datetime.now(timezone.utc).timestamp() * 1000),
            'messages': [{'messageTypeId': 244, 'targetId': 11, 'from': 1, 'to': 2},
                         {'messageTypeId': 244, 'targetId': 21, 'from': 2, 'to': 1}], **kwargs}


def source(monkeypatch, data):
    mock = Mock(return_value=data)
    monkeypatch.setattr(espn_read, 'read_league', mock)
    return mock


def test_pending_keeps_accepted_distinct_from_completed_and_sanitizes(researcher, monkeypatch):
    source(monkeypatch, {'pendingTransactions': [pending()]})
    result = t.execute('get_pending_trades', {}, researcher)
    row = result['trades'][0]
    assert row['state'] == 'accepted_awaiting_processing'
    assert row['completed'] is False
    assert row['items'][0] == {'player_id': '11', 'player_name': 'Alpha', 'type': 'TRADE',
                              'from_team_id': '1', 'to_team_id': '2'}
    assert row['team_ids'] == ['1', '2']
    assert 'configured ESPN account' in result['visibility']
    serialized = json.dumps(result)
    for excluded in ('12345', 'secret-member-id', 'private trade comment', 'test-cookie', 'test-owner', 'Oak', 'Maple'):
        assert excluded not in serialized


@pytest.mark.parametrize('raw,expected', [
    ({}, 'not_exposed'), ({'pendingTransactions': []}, 'no_visible_trade_records'),
    ({'pendingTransactions': None}, 'not_exposed'),
])
def test_absence_is_not_proof_no_pending_trades(researcher, monkeypatch, raw, expected):
    source(monkeypatch, raw)
    result = t.execute('get_pending_trades', {}, researcher)
    assert result['status'] == expected
    assert result['trades'] == []
    assert 'not a league-wide inventory' in result['visibility']
    assert result['visible_count'] is (None if expected == 'not_exposed' else 0)


def test_pending_waivers_never_reach_model(researcher, monkeypatch):
    waiver = pending(type='WAIVER', items=[{'type': 'ADD', 'playerId': 91, 'toTeamId': 1}])
    source(monkeypatch, {'pendingTransactions': [waiver]})
    assert t.execute('get_pending_trades', {}, researcher)['trades'] == []


@pytest.mark.parametrize('kwargs,state', [
    ({'acceptedDate': None}, 'proposed'),
    ({'acceptedDate': None, 'status': 'VETOED'}, 'closed_without_verified_completion'),
    ({'type': 'TRADE_ACCEPT', 'status': 'EXECUTED', 'isPending': False}, 'accepted_awaiting_processing'),
    ({'type': 'TRADE', 'status': 'UNKNOWN', 'acceptedDate': None}, 'pending_state_unconfirmed'),
])
def test_pending_lifecycle_never_promoted_to_completion(researcher, monkeypatch, kwargs, state):
    source(monkeypatch, {'pendingTransactions': [pending(**kwargs)]})
    row = t.execute('get_pending_trades', {}, researcher)['trades'][0]
    assert row['state'] == state
    assert row['completed'] is False


def test_missing_items_or_unknown_teams_do_not_invent_transfer_directions(researcher, monkeypatch):
    source(monkeypatch, {'pendingTransactions': [pending(items=[], teamId=1),
        pending(proposedDate=1790190000100, items=[{'type': 'TRADE', 'playerId': 91, 'fromTeamId': 99, 'toTeamId': 1}])]})
    rows = t.execute('get_pending_trades', {'team_id': '1'}, researcher)['trades']
    assert len(rows) == 2
    assert any(row['items'] == [] and row['items_complete'] is False for row in rows)
    item = next(row['items'][0] for row in rows if row['items'])
    assert item['from_team_id'] is None and item['to_team_id'] == '1'


def test_pending_reads_fixed_view_once_per_request_with_different_limits(researcher, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': [pending()]})
    t.execute('get_pending_trades', {'limit': 1}, researcher)
    t.execute('get_pending_trades', {'limit': 20}, researcher)
    read.assert_called_once()
    assert read.call_args.args == (researcher.league, ['mPendingTransactions'])
    assert read.call_args.kwargs['params'] == {'scoringPeriodId': 3}
    assert read.call_args.kwargs['max_bytes'] == 1_000_000


def test_completed_activity_has_exact_directions_deduplicates_and_ignores_other_messages(researcher, monkeypatch):
    first = activity()
    duplicate = deepcopy(first)
    duplicate['messages'].reverse()
    unrelated = activity(messages=[{'messageTypeId': 178, 'targetId': 91, 'from': 999, 'to': 1}])
    read = source(monkeypatch, {'topics': [first, duplicate, unrelated]})
    result = t.execute('get_recent_trades', {}, researcher)
    assert len(result['trades']) == 1
    row = result['trades'][0]
    assert row['completed'] and row['state'] == 'completed'
    assert row['directions_complete'] and row['team_ids'] == ['1', '2']
    assert row['items'][0]['from_team_id'] == '1'
    assert row['items'][0]['to_team_id'] == '2'
    assert read.call_args.kwargs['path'] == '/communication/'
    assert read.call_args.kwargs['filters']['topics']['filterIncludeMessageTypeIds']['value'] == [244]
    assert result['scan_complete'] and not result['truncated']


def test_completed_scan_date_window_and_bounds(researcher, monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    read = source(monkeypatch, {'topics': [activity(stamp=now - i) for i in range(25)]})
    result = t.execute('get_recent_trades', {'limit': 2}, researcher)
    assert len(result['trades']) == 2
    assert result['truncated'] and not result['scan_complete']
    read.assert_called_once()
    researcher.transaction_cache.clear()
    read.return_value = {'topics': [activity(stamp=now - 61 * 86400000), activity(stamp=now + 86400000)]}
    result = t.execute('get_recent_trades', {'days': 60}, researcher)
    assert result['trades'] == []


def test_history_retains_trade_acceptance_event_without_claiming_completed(researcher, monkeypatch):
    accepted = pending(type='TRADE_ACCEPT', status='EXECUTED', items=[], processDate=1790190150000)
    read = source(monkeypatch, {'transactions': [accepted]})
    row = t.execute('get_transaction_history', {}, researcher)['transactions'][0]
    assert row['type'] == 'TRADE_ACCEPT'
    assert 'Not established' in row['trade_completion']
    assert not row['items_complete']
    assert 'completed' not in row
    assert read.call_args.kwargs['params'] == {'scoringPeriodId': 3}
    assert read.call_args.args[1] == ['mTransactions2']


def test_history_excludes_private_pending_and_failed_waivers_and_bids(researcher, monkeypatch):
    items = [{'type': 'ADD', 'playerId': 91, 'fromTeamId': -1, 'toTeamId': 1}]
    rows = [pending(type='WAIVER', status=status, items=items, isPending=status == 'PENDING')
            for status in ('PENDING', 'FAILED', 'EXECUTED')]
    source(monkeypatch, {'transactions': rows})
    result = t.execute('get_transaction_history', {}, researcher)
    assert len(result['transactions']) == 1
    assert result['transactions'][0]['source_status'] == 'EXECUTED'
    assert 'bidAmount' not in json.dumps(result)
    assert '12345' not in json.dumps(result)


def trade_args(**kwargs):
    return {'team_a_id': '1', 'team_b_id': '2', 'team_a_player_ids': ['11'],
            'team_b_player_ids': ['21'], **kwargs}


def test_proposal_projects_correct_directions_without_mutating_rosters_or_network(researcher, monkeypatch):
    read = source(monkeypatch, {})
    before = [[p.playerId for p in team.roster] for team in researcher.league.teams]
    result = t.execute('evaluate_trade_proposal', trade_args(), researcher)
    assert result['kind'] == 'hypothetical_trade' and result['completed'] is False
    a, b = result['teams']
    assert a['starter_projection_change'] == -8
    assert b['starter_projection_change'] == 8
    assert a['sent_player_ids'] == ['11'] and a['received_player_ids'] == ['21']
    assert [[p.playerId for p in team.roster] for team in researcher.league.teams] == before
    read.assert_not_called()


@pytest.mark.parametrize('args', [trade_args(team_b_id='1'), trade_args(team_a_player_ids=['21']),
                                  trade_args(team_a_player_ids=['99'])])
def test_proposal_rejects_wrong_ownership_or_self_trade(researcher, args):
    assert 'error' in t.execute('evaluate_trade_proposal', args, researcher)


def test_proposal_missing_projection_or_rules_is_unknown_gain(researcher):
    result = t.execute('evaluate_trade_proposal', trade_args(week=4), researcher)
    assert all(row['starter_projection_change'] is None for row in result['teams'])
    researcher.context = {}
    result = t.execute('evaluate_trade_proposal', trade_args(), researcher)
    assert all(row['starter_projection_change'] is None for row in result['teams'])


@pytest.mark.parametrize('name,args', [
    ('get_pending_trades', {'url': 'https://example.com'}),
    ('get_pending_trades', {'team_id': '999'}),
    ('get_recent_trades', {'days': True}),
    ('get_recent_trades', {'limit': 21}),
    ('get_recent_trades', {'days': 0}),
    ('get_recent_trades', []),
    ('get_transaction_history', {'week': 4}),
    ('get_transaction_history', {'types': ['WAIVER_ERROR']}),
    ('get_transaction_history', {'types': [{'bad': 'object'}]}),
    ('evaluate_trade_proposal', trade_args(team_a_player_ids=['11', '11'])),
    ('evaluate_trade_proposal', trade_args(team_a_player_ids=[11])),
    ('evaluate_trade_proposal', trade_args(week=2)),
    ('evaluate_trade_proposal', trade_args(week=7)),
])
def test_invalid_arguments_never_make_network_requests(researcher, monkeypatch, name, args):
    read = source(monkeypatch, {})
    assert 'error' in t.execute(name, args, researcher)
    read.assert_not_called()


def test_errors_do_not_expose_source_exceptions_or_credentials(researcher, monkeypatch):
    read = source(monkeypatch, {})
    read.side_effect = ValueError('espn_s2=test-cookie-never-return private-response')
    result = t.execute('get_pending_trades', {}, researcher)
    assert result == {'error': 'ESPN transaction research is unavailable; missing information is unknown.'}


def test_expired_deadline_makes_no_request(researcher, monkeypatch):
    read = source(monkeypatch, {})
    researcher.deadline = time.monotonic() + 10
    assert 'error' in t.execute('get_pending_trades', {}, researcher)
    read.assert_not_called()


def test_historical_reports_exclude_current_offers_and_future_events(researcher, monkeypatch):
    read = source(monkeypatch, {'transactions': []})
    researcher.context['historical'] = True
    researcher.week = 2
    assert 'error' in t.execute('get_pending_trades', {}, researcher)
    assert 'error' in t.execute('get_recent_trades', {}, researcher)
    assert 'error' in t.execute('evaluate_trade_proposal', trade_args(week=3), researcher)
    assert 'error' in t.execute('get_transaction_history', {'week': 3}, researcher)
    read.assert_not_called()
    assert t.execute('get_transaction_history', {'week': 2}, researcher)['transactions'] == []


def test_awareness_capped_shared_deadline_and_cache(researcher, monkeypatch):
    proposals = [pending(proposedDate=1790190000000 + i) for i in range(5)]
    read = source(monkeypatch, {})
    read.side_effect = [{'pendingTransactions': proposals}, {'topics': [activity()]}]
    cache = {}
    snapshot = t.build_trade_awareness(researcher.league, cache=cache)
    assert len(snapshot['pending']['trades']) == 3
    assert snapshot['pending']['truncated']
    assert len(snapshot['recent_completed']['trades']) == 1
    assert read.call_count == 2
    deadlines = [call.kwargs['deadline'] for call in read.call_args_list]
    assert deadlines[0] == deadlines[1]
    assert all(call.kwargs['timeout'] == 3.5 for call in read.call_args_list)
    assert len(cache) == 2
    second = t.build_trade_awareness(researcher.league, cache=cache)
    assert second['pending']['trades'] == snapshot['pending']['trades']
    assert read.call_count == 2


def test_awareness_failure_is_nonfatal_and_not_empty_success(researcher, monkeypatch):
    read = source(monkeypatch, {})
    read.side_effect = TimeoutError('secret detail')
    snapshot = t.build_trade_awareness(researcher.league)
    assert snapshot['pending']['status'] == 'unavailable'
    assert snapshot['recent_completed']['status'] == 'unavailable'
    assert 'secret detail' not in json.dumps(snapshot)


def test_tool_contract_is_strict_and_read_only():
    assert len(t.NAMES) == 4
    for definition in t.TOOLS:
        schema = definition['function']['parameters']
        assert schema['additionalProperties'] is False
        assert 'url' not in schema['properties']
        assert 'view' not in schema['properties']


def test_transaction_history_discards_rows_from_different_week(researcher, monkeypatch):
    source(monkeypatch, {'transactions': [pending(type='TRADE_ACCEPT', scoringPeriodId=4)]})
    result = t.execute('get_transaction_history', {'week': 3}, researcher)
    assert result['transactions'] == []


def test_completed_activity_discloses_cutoff_within_large_transaction(researcher, monkeypatch):
    source(monkeypatch, {'topics': [activity(messages=[
        {'messageTypeId': 244, 'targetId': pid, 'from': 1, 'to': 2}
        for pid in range(100, 150)])]})
    row = t.execute('get_recent_trades', {}, researcher)['trades'][0]
    assert len(row['items']) == 40
    assert row['items_complete'] is False
    assert row['directions_complete'] is False


def test_awareness_compacts_large_trades_without_claiming_all_legs(researcher, monkeypatch):
    many = pending(items=[{'type': 'TRADE', 'playerId': pid, 'fromTeamId': 1, 'toTeamId': 2}
                           for pid in range(100, 112)])
    read = source(monkeypatch, {})
    read.side_effect = [{'pendingTransactions': [many]}, {'topics': []}]
    row = t.build_trade_awareness(researcher.league)['pending']['trades'][0]
    assert len(row['items']) == 6
    assert row['omitted_items'] == 6
    assert row['items_complete'] is False


def _context_dependencies(monkeypatch):
    from gamedaybot.espn import analyst_evidence, community, league_tools, nfl_usage, research
    monkeypatch.setattr(analyst_evidence, 'rules', lambda league: {})
    monkeypatch.setattr(analyst_evidence, 'history', lambda *args, **kwargs: {})
    legacy = Mock(return_value=[])
    monkeypatch.setattr(analyst_evidence, 'recent_trade_history', legacy)
    monkeypatch.setattr(analyst_evidence, 'forecast_memory', lambda *args: [])
    monkeypatch.setattr(community, 'nfl_games', lambda league: {})
    monkeypatch.setattr(nfl_usage, 'usage_context', lambda *args: ({}, {}))
    monkeypatch.setattr(research, 'news_feed', lambda: ([], 'now'))
    schedule = Mock(return_value={'teams': []})
    monkeypatch.setattr(league_tools, 'build_schedule_awareness', schedule)
    awareness = Mock(return_value={'pending': {'status': 'not_exposed', 'trades': []}})
    monkeypatch.setattr(t, 'build_trade_awareness', awareness)
    return awareness, schedule, legacy


@pytest.mark.parametrize('report', ['get_standings', 'get_matchups', 'get_power_rankings', 'get_trade_report'])
def test_default_context_includes_trade_and_schedule_awareness_without_duplicate_history(researcher, monkeypatch, report):
    from gamedaybot.espn import research
    awareness, schedule, legacy = _context_dependencies(monkeypatch)
    result = research.build_context(researcher.league, '', report, week=3)
    assert result['trade_awareness']['pending']['status'] == 'not_exposed'
    assert result['schedule_awareness'] == {'teams': []}
    awareness.assert_called_once()
    assert awareness.call_args.kwargs['deadline'] <= time.monotonic() + 8
    schedule.assert_called_once_with(researcher.league, week=3)
    legacy.assert_not_called()
    assert 'recent_trades' not in result['league_history']


@pytest.mark.parametrize('kwargs', [{'week': 2}, {'week': 3, 'requested_player_ids': {'11'}}])
def test_default_awareness_excluded_for_historical_and_player_only_context(researcher, monkeypatch, kwargs):
    from gamedaybot.espn import research
    awareness, schedule, legacy = _context_dependencies(monkeypatch)
    result = research.build_context(researcher.league, '', 'get_matchups', **kwargs)
    assert 'trade_awareness' not in result and 'schedule_awareness' not in result
    awareness.assert_not_called()
    schedule.assert_not_called()


def test_fixture_without_verified_league_identity_does_not_fetch_trade_awareness(researcher, monkeypatch):
    from gamedaybot.espn import research
    awareness, _, _ = _context_dependencies(monkeypatch)
    del researcher.league.league_id
    research.build_context(researcher.league, '', 'get_standings', week=3)
    awareness.assert_not_called()
