from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import espn_read, transaction_updates as updates


BASELINE = '2026-09-24T12:00:00+00:00'
LATER = '2026-09-24T13:00:00+00:00'
NEXT = '2026-09-24T14:00:00+00:00'


def millis(stamp):
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


@pytest.fixture
def league():
    return O(year=2026, league_id=123, scoringPeriodId=3,
             teams=[O(team_id=1, team_name='Oak', roster=[O(playerId=11, name='Alpha')]),
                    O(team_id=2, team_name='Maple', roster=[O(playerId=21, name='Gamma')])],
             player_map={91: 'Free Agent'}, espn_request=O(cookies={'espn_s2': 'never-return'}))


def pending(**kwargs):
    return {'id': 'proposal-1', 'type': 'TRADE_PROPOSAL', 'status': 'PENDING',
            'proposedDate': millis(BASELINE) - 60000, 'acceptedDate': None, 'isPending': True,
            'bidAmount': 999, 'memberId': 'private-member', 'comment': 'private-comment',
            'items': [{'type': 'TRADE', 'playerId': 11, 'fromTeamId': 1, 'toTeamId': 2},
                      {'type': 'TRADE', 'playerId': 21, 'fromTeamId': 2, 'toTeamId': 1}], **kwargs}


def roster_move(**kwargs):
    return {'id': 'move-1', 'type': 'FREEAGENT', 'status': 'EXECUTED', 'scoringPeriodId': 3,
            'processDate': millis(LATER) - 1000, 'isPending': False,
            'bidAmount': 123, 'memberId': 'private-member', 'comment': 'private-comment',
            'items': [{'type': 'ADD', 'playerId': 91, 'fromTeamId': -1, 'toTeamId': 1},
                      {'type': 'DROP', 'playerId': 11, 'fromTeamId': 1, 'toTeamId': -1}], **kwargs}


def source(monkeypatch, data):
    read = Mock(return_value=data)
    monkeypatch.setattr(espn_read, 'read_league', read)
    return read


def test_proposals_baseline_is_quiet_and_new_offer_announces_once(league, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': [pending()]})
    events, state = updates.collect_proposals(league, observed_at=BASELINE)
    assert events == [] and len(state['seen']) == 1
    previous = deepcopy(state)
    read.return_value = {'pendingTransactions': [pending(), pending(id='proposal-2', proposedDate=millis(LATER) - 1000)]}
    events, following = updates.collect_proposals(league, state, LATER)
    assert len(events) == 1 and events[0]['kind'] == 'trade_proposal'
    assert events[0]['change'] == 'newly_visible'
    assert events[0]['trade']['team_ids'] == ['1', '2']
    assert events[0]['trade']['items_complete']
    assert events[0]['trade']['items'][0]['from_team_id'] == '1'
    assert events[0]['trade']['items'][0]['to_team_id'] == '2'
    assert state == previous
    assert updates.collect_proposals(league, json.loads(json.dumps(following)), NEXT)[0] == []
    assert read.call_args.args == (league, ['mPendingTransactions'])
    assert read.call_args.kwargs['max_bytes'] == 1_000_000


def test_proposal_lifecycle_accepted_is_never_completed_and_states_do_not_repeat(league, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': [pending()]})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    read.return_value = {'pendingTransactions': [pending(type='TRADE_ACCEPT', status='EXECUTED',
                                                       isPending=False, acceptedDate=millis(LATER) - 1000)]}
    events, state = updates.collect_proposals(league, state, LATER)
    accepted = events[0]
    assert accepted['change'] == 'status_changed'
    assert accepted['trade']['state'] == 'accepted_awaiting_processing'
    assert accepted['trade']['completed'] is False
    identity = accepted['trade']['id']
    read.return_value = {'pendingTransactions': [pending(status='VETOED')]}
    events, state = updates.collect_proposals(league, state, NEXT)
    closed = events[0]
    assert closed['trade']['id'] == identity
    assert closed['id'] != accepted['id']
    assert closed['trade']['state'] == 'closed_without_verified_completion'
    assert closed['trade']['completed'] is False
    read.return_value = {'pendingTransactions': [pending()]}
    assert updates.collect_proposals(league, state, NEXT)[0] == []


def test_proposal_disappearance_and_reappearance_never_announces_decline_or_completion(league, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': [pending()]})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    read.return_value = {'pendingTransactions': []}
    events, absent = updates.collect_proposals(league, state, LATER)
    assert events == [] and absent['seen'] == state['seen']
    read.return_value = {'pendingTransactions': [pending()]}
    assert updates.collect_proposals(league, absent, NEXT)[0] == []


@pytest.mark.parametrize('failure', [{}, {'pendingTransactions': None}, TimeoutError('never-return')])
def test_proposal_outage_preserves_state_and_recovery_can_announce_new_offer(league, monkeypatch, failure):
    read = source(monkeypatch, {'pendingTransactions': []})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    before = deepcopy(state)
    if isinstance(failure, Exception):
        read.side_effect = failure
    else:
        read.return_value = failure
    with pytest.raises(updates.TransactionUpdateUnavailable) as caught:
        updates.collect_proposals(league, state, LATER)
    assert state == before and 'never-return' not in str(caught.value)
    read.side_effect = None
    read.return_value = {'pendingTransactions': [pending(proposedDate=millis(LATER))]}
    assert len(updates.collect_proposals(league, state, NEXT)[0]) == 1


def test_proposal_unknown_status_does_not_fabricate_an_offer(league, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': []})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    read.return_value = {'pendingTransactions': [pending(type='TRADE', status='UNKNOWN')]}
    events, state = updates.collect_proposals(league, state, LATER)
    assert events == [] and state['unconfirmed_count'] == 1
    read.return_value = {'pendingTransactions': [pending(type='TRADE', status='PENDING')]}
    events, state = updates.collect_proposals(league, state, NEXT)
    assert len(events) == 1
    assert events[0]['trade']['completed'] is False


def test_proposal_truncation_keeps_previous_records_and_warns_in_state(league, monkeypatch):
    read = source(monkeypatch, {'pendingTransactions': [pending()]})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    read.return_value = {'pendingTransactions': [pending(proposedDate=millis(LATER) + n) for n in range(101)]}
    events, state = updates.collect_proposals(league, state, LATER)
    assert len(events) == 100 and len(state['seen']) == 101
    assert state['truncated'] is True
    assert 'not a league-wide inventory' in state['visibility']


def test_proposal_ledger_compaction_does_not_reannounce_evicted_old_records(league, monkeypatch):
    monkeypatch.setattr(updates, '_PROPOSAL_LIMIT', 2)
    read = source(monkeypatch, {'pendingTransactions': [pending(proposedDate=millis(BASELINE) - n * 1000) for n in range(3)]})
    _, state = updates.collect_proposals(league, observed_at=BASELINE)
    assert len(state['seen']) == 2 and state['ledger_compacted']
    assert updates.collect_proposals(league, state, LATER)[0] == []


def test_roster_baseline_new_completed_moves_and_exactly_once_across_rollover(league, monkeypatch):
    read = source(monkeypatch, {'transactions': [roster_move(id='old', processDate=millis(BASELINE) - 1000)]})
    events, state = updates.collect_roster_moves(league, observed_at=BASELINE)
    assert events == []
    assert state['scanned_weeks'] == [2, 3]
    before = deepcopy(state)
    read.return_value = {'transactions': [roster_move(), roster_move(id='waiver-1', type='WAIVER')]}
    events, state = updates.collect_roster_moves(league, state, LATER)
    assert len(events) == 2
    assert len({event['id'] for event in events}) == 2
    assert all(event['transaction']['completed'] for event in events)
    assert events[0]['transaction']['items'][0]['from_team_id'] is None
    assert events[0]['transaction']['items'][0]['to_team_id'] == '1'
    assert len(before['seen']) == 1
    league.scoringPeriodId = 4
    repeated, state = updates.collect_roster_moves(league, json.loads(json.dumps(state)), NEXT)
    assert repeated == [] and state['scanned_weeks'] == [3, 4]
    assert len(state['seen']) == 3


def test_roster_only_completed_add_drop_transactions_are_visible(league, monkeypatch):
    read = source(monkeypatch, {'transactions': []})
    _, state = updates.collect_roster_moves(league, observed_at=BASELINE)
    read.return_value = {'transactions': [
        roster_move(id='pending', type='WAIVER', status='PENDING', isPending=True),
        roster_move(id='failed', type='WAIVER', status='FAILED'),
        roster_move(id='lineup', type='ROSTER'), roster_move(id='trade', type='TRADE_ACCEPT'),
        roster_move(id='not-add', items=[{'type': 'MOVE', 'playerId': 11, 'fromTeamId': 1, 'toTeamId': 1}]),
        roster_move(id='add-drop', status='COMPLETED'),
    ]}
    events, state = updates.collect_roster_moves(league, state, LATER)
    assert len(events) == 1
    assert events[0]['transaction']['type'] == 'FREEAGENT'
    assert events[0]['transaction']['source_status'] == 'COMPLETED'
    assert [item['type'] for item in events[0]['transaction']['items']] == ['ADD', 'DROP']
    for call in read.call_args_list:
        assert call.kwargs['filters'] == {'transactions': {'filterType': {'value': ['FREEAGENT', 'WAIVER']}}}


def test_old_or_undated_roster_history_does_not_announce_on_reappearance(league, monkeypatch):
    read = source(monkeypatch, {'transactions': []})
    _, state = updates.collect_roster_moves(league, observed_at=BASELINE)
    read.return_value = {'transactions': [roster_move(id='old', processDate=millis(BASELINE) - 1000),
                                        roster_move(id='undated', processDate=None)]}
    events, state = updates.collect_roster_moves(league, state, LATER)
    assert events == [] and state['undated_count'] == 1


def test_roster_second_week_failure_is_atomic_and_recovers(league, monkeypatch):
    read = source(monkeypatch, {'transactions': []})
    _, state = updates.collect_roster_moves(league, observed_at=BASELINE)
    before = deepcopy(state)
    read.side_effect = [{'transactions': [roster_move()]}, {}]
    with pytest.raises(updates.TransactionUpdateUnavailable):
        updates.collect_roster_moves(league, state, LATER)
    assert state == before
    read.side_effect = None
    read.return_value = {'transactions': [roster_move()]}
    assert len(updates.collect_roster_moves(league, state, NEXT)[0]) == 1


@pytest.mark.parametrize('collector,raw', [(updates.collect_proposals, {'pendingTransactions': [pending()]}),
                                         (updates.collect_roster_moves, {'transactions': [roster_move()]})])
def test_state_and_events_never_include_private_fields_and_season_change_rebaselines(league, monkeypatch, collector, raw):
    read = source(monkeypatch, {key: [] for key in raw})
    _, state = collector(league, observed_at=BASELINE)
    read.return_value = raw
    events, state = collector(league, state, LATER)
    assert events
    serialized = json.dumps([events, state])
    for forbidden in ('bidAmount', 'memberId', 'private-member', 'private-comment', 'never-return', 'Oak', 'Maple'):
        assert forbidden not in serialized
    league.year = 2027
    assert collector(league, state, NEXT)[0] == []


def test_roster_week_one_does_not_fetch_invalid_previous_week(league, monkeypatch):
    league.scoringPeriodId = 1
    read = source(monkeypatch, {'transactions': []})
    _, state = updates.collect_roster_moves(league, observed_at=datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
    read.assert_called_once()
    assert state['scanned_weeks'] == [1]


def test_invalid_scoring_week_does_not_query_espn(league, monkeypatch):
    league.scoringPeriodId = None
    read = source(monkeypatch, {'transactions': []})
    with pytest.raises(updates.TransactionUpdateUnavailable):
        updates.collect_roster_moves(league, observed_at=BASELINE)
    read.assert_not_called()
