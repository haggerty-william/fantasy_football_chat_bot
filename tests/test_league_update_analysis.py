from copy import deepcopy
import json
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import analysis, analyst_evidence, community, nfl_usage, research, transaction_tools
from gamedaybot.espn.analysis_packet import AnalysisPacket
from gamedaybot.espn.commentary_checks import check_commentary
from gamedaybot.espn.transaction_checks import check_transaction_claims


ENDPOINT = 'http://localhost:1234/v1/chat/completions'
OBSERVED = '2026-09-24T12:00:00+00:00'


def injury(status='OUT', previous='QUESTIONABLE', **kwargs):
    return {'id': 'injury-11-out', 'kind': 'injury_status', 'observed_at': OBSERVED,
            'player_id': '11', 'player_name': 'Alpha Receiver', 'team_id': '1', 'team': 'Oak',
            'previous_status': previous, 'status': status, **kwargs}


def proposal(state='proposed'):
    return {'id': 'proposal-one', 'kind': 'trade_proposal', 'observed_at': OBSERVED,
            'trade': {'id': 'opaque-id', 'state': state, 'source_status': 'PENDING', 'completed': False,
                      'is_pending': True, 'proposed_at': OBSERVED, 'items_complete': True, 'team_ids': ['1', '2'],
                      'items': [{'player_id': '11', 'player_name': 'Alpha Receiver', 'type': 'TRADE', 'from_team_id': '1', 'to_team_id': '2'},
                                {'player_id': '21', 'player_name': 'Beta Runner', 'type': 'TRADE', 'from_team_id': '2', 'to_team_id': '1'}]}}


def roster_move():
    return {'id': 'move-one', 'kind': 'roster_move', 'observed_at': OBSERVED,
            'transaction': {'type': 'FREEAGENT', 'source_status': 'EXECUTED', 'scoring_week': 3,
                            'processed_at': OBSERVED, 'team_ids': ['1'], 'items_complete': True,
                            'items': [{'player_id': '91', 'player_name': 'Available Back', 'type': 'ADD',
                                       'from_team_id': None, 'to_team_id': '1'}]}}


@pytest.fixture
def league(monkeypatch):
    def player(pid, name):
        return O(playerId=pid, name=name, position='WR', lineupSlot='WR', injuryStatus='OUT' if pid == 11 else 'QUESTIONABLE',
                 stats={3: {'projected_points': 12}}, proTeam='BUF', eligibleSlots=['WR'], schedule={})
    teams = [O(team_id=1, team_name='Oak', owners=[{'firstName': 'Alex', 'lastName': 'Morgan'}],
               roster=[player(11, 'Alpha Receiver')]),
             O(team_id=2, team_name='Maple', owners=[{'firstName': 'Bobby', 'lastName': 'Example'}],
               roster=[player(21, 'Beta Runner')]),
             O(team_id=3, team_name='Birch', owners=[], roster=[player(31, 'Unaffected Player')])]
    value = O(year=2026, league_id=123, scoringPeriodId=3, teams=teams, espn_request=O(),
               settings=O(matchup_periods={str(w): [w] for w in range(1, 15)}))
    monkeypatch.setattr(analyst_evidence, 'rules', lambda *args: {'lineup_slots': {'WR': 1}})
    monkeypatch.setattr(analyst_evidence, 'history', lambda *args, **kwargs: {'teams': []})
    monkeypatch.setattr(analyst_evidence, 'forecast_memory', lambda *args: [])
    monkeypatch.setattr(community, 'nfl_games', lambda *args: {})
    monkeypatch.setattr(nfl_usage, 'usage_context', lambda *args: ({}, {}))
    monkeypatch.setattr(research, 'news_feed', lambda: ([], OBSERVED))
    # Later current views can be empty: the event observation must survive.
    monkeypatch.setattr(transaction_tools, 'build_trade_awareness', lambda *args, **kwargs: {
        'pending': {'status': 'no_visible_trade_records', 'trades': []}, 'recent_completed': {'trades': []}})
    return value


def context(league, events):
    return research.build_context(league, 'League updates', 'get_league_updates', week=3, event_facts=events)


def test_observed_status_and_current_snapshot_focus_on_affected_team(league):
    data = context(league, [injury()])
    assert [row['team'] for row in data['rosters']] == ['Oak']
    assert [row['id'] for row in data['players']] == [11]
    assert data['players'][0]['current_status'] == 'OUT'
    assert data['players'][0]['event_status_observed_at'] == OBSERVED
    assert data['rosters'][0]['players'][0][6] == 'OUT'
    assert 'not the time an injury occurred' in data['event_scope']
    assert league.teams[0].roster[0].injuryStatus == 'OUT'
    assert 'observed_status_mismatch' in check_transaction_claims('Alpha Receiver is questionable.', data)
    assert 'unsupported_health_inference' in check_transaction_claims('Alpha Receiver is healthy.', data)
    assert check_transaction_claims('Alpha Receiver is out.', data) == []


@pytest.mark.parametrize('status', ['ACTIVE', 'UNKNOWN'])
def test_active_or_unknown_never_establishes_health(league, status):
    league.teams[0].roster[0].injuryStatus = status
    data = context(league, [injury(status=status, previous='OUT')])
    assert data['players'][0]['current_status'] == status
    assert 'unsupported_health_inference' in check_commentary('Alpha Receiver is healthy.', 'League updates', data)
    assert 'unsupported_medical_prognosis' in check_transaction_claims('Alpha Receiver will recover soon.', data)
    assert 'does not prove full health' in analysis.INSTRUCTIONS


@pytest.mark.parametrize('status', ['PROBABLE', 'NON_FOOTBALL_INJURY', 'NON_FOOTBALL_ILLNESS', 'PUP'])
def test_known_collector_designations_are_preserved(league, status):
    league.teams[0].roster[0].injuryStatus = status
    data = context(league, [injury(status=status)])
    assert data['event_facts'][0]['status'] == status
    assert data['players'][0]['current_status'] == status


@pytest.mark.parametrize('raw,canonical', [('NORMAL', 'ACTIVE'), ('IR', 'INJURY_RESERVE'),
    ('SUSPENDED', 'SUSPENSION'), ('DTD', 'DAY_TO_DAY'), ('PHYSICALLY_UNABLE_TO_PERFORM', 'PUP')])
def test_current_roster_aliases_use_same_canonical_designations_as_collector(league, raw, canonical):
    league.teams[0].roster[0].injuryStatus = raw
    data = context(league, [injury(status=canonical)])
    assert data['event_facts'][0]['current_snapshot_status'] == canonical
    assert data['players'][0]['current_status'] == canonical


def test_observed_event_batch_bounds_prevent_unbounded_context(league):
    with pytest.raises(ValueError, match='one to twenty'):
        research.normalize_event_facts(league, [injury(id='event-' + str(index)) for index in range(21)])
    rows = []
    for index in range(20):
        event = proposal()
        event['id'] = 'large-proposal-' + str(index)
        event['trade']['items'] = [{**event['trade']['items'][0], 'player_id': str(pid), 'player_name': 'A' * 100}
                                   for pid in range(100, 120)]
        rows.append(event)
    with pytest.raises(ValueError, match='batch limit'):
        research.normalize_event_facts(league, rows)


def test_player_only_refresh_keeps_observed_status_without_losing_other_requested_teams(league):
    data = research.build_context(league, '', 'get_matchups', 3, requested_player_ids={'11', '21'}, event_facts=[injury()])
    by_id = {str(player['id']): player for player in data['players']}
    assert by_id['11']['current_status'] == 'OUT'
    assert by_id['21']['current_status'] == 'QUESTIONABLE'


def test_observed_prior_to_new_status_language_remains_available(league):
    data = context(league, [injury()])
    assert data['event_facts'][0]['previous_status'] == 'QUESTIONABLE'
    assert data['event_facts'][0]['status'] == 'OUT'
    assert check_commentary('Alpha Receiver changed from questionable to out between observations.', 'League updates', data) == []


def test_newer_explicit_player_status_can_supersede_event_observation(league):
    data = context(league, [injury()])
    data['players'][0]['current_status'] = 'ACTIVE'
    assert check_transaction_claims('Alpha Receiver is active.', data) == []
    assert data['event_facts'][0]['status'] == 'OUT'


def test_queued_out_event_never_overrides_fresh_active_roster_even_after_pruning(league):
    league.teams[0].roster[0].injuryStatus = 'ACTIVE'
    data = context(league, [injury()])
    assert data['players'][0]['current_status'] == 'ACTIVE'
    assert data['event_facts'][0]['status'] == 'OUT'
    assert data['event_facts'][0]['current_snapshot_status'] == 'ACTIVE'
    assert check_transaction_claims('Alpha Receiver is active.', data) == []
    assert 'observed_status_mismatch' in check_transaction_claims('Alpha Receiver is out.', data)
    data['players'] = []
    data['rosters'] = []
    assert check_transaction_claims('Alpha Receiver is active.', data) == []
    assert 'observed_status_mismatch' in check_transaction_claims('Alpha Receiver is out.', data)


def test_unknown_roster_can_retain_explicitly_dated_last_event_designation(league):
    league.teams[0].roster[0].injuryStatus = 'UNKNOWN'
    data = context(league, [injury()])
    assert data['event_facts'][0]['current_snapshot_status'] == 'UNKNOWN'
    assert data['players'][0]['current_status'] == 'OUT'
    assert data['players'][0]['status_observed_at'] == OBSERVED
    assert 'Last dated event observation only' in data['players'][0]['status_scope']


def test_event_packet_keeps_names_once_and_moves_leg_data_to_canonical_teams(league):
    data = context(league, [injury(), proposal(), roster_move()])
    packet = AnalysisPacket(league, data, 'Oak and Maple league updates', 'get_league_updates', 3, OBSERVED)
    raw = packet.dumps()
    for name in ('Oak', 'Maple', 'Alex Morgan', 'Bobby Example'):
        assert raw.count(name) == 1
    canonical = json.loads(raw)['research_context']
    assert 'event_facts' not in canonical
    assert canonical['observed_event_index']['proposal-one']['completed'] is False
    assert canonical['observed_event_index']['proposal-one']['state'] == 'proposed'
    assert canonical['observed_event_index']['injury-11-out']['observed_at'] == OBSERVED
    by_team = canonical['teams']
    assert by_team['1']['observed_events'][0]['status'] == 'OUT'
    a = next(row for row in by_team['1']['observed_events'] if row['event_id'] == 'proposal-one')
    b = next(row for row in by_team['2']['observed_events'] if row['event_id'] == 'proposal-one')
    assert a['items'][0]['direction'] == 'sent' and b['items'][0]['direction'] == 'received'
    assert 'observed_events' not in by_team['3']
    assert not data.get('trade_sides')
    assert all('trades' not in row for row in by_team.values())


@pytest.mark.parametrize('state', ['proposed', 'accepted_awaiting_processing', 'closed_without_verified_completion'])
def test_proposal_observation_guards_survive_absent_current_offer(league, state):
    data = context(league, [proposal(state)])
    assert 'pending_trade_as_completed' in check_commentary('Maple acquired Alpha Receiver.', 'League updates', data)
    assert check_transaction_claims('If the offer clears, Maple would receive Alpha Receiver.', data) == []
    if state == 'closed_without_verified_completion':
        assert data['event_facts'][0]['trade']['actionable'] is False
        assert 'closed_trade_as_actionable' in check_transaction_claims('Maple will receive Alpha Receiver.', data)


def test_event_facts_survive_pruning_before_optional_forecasts_and_rosters(league, monkeypatch):
    monkeypatch.setenv('AI_CONTEXT_CHAR_LIMIT', '16000')
    monkeypatch.setattr(analyst_evidence, 'forecast_memory', lambda *args: [{'old_forecast': 'old ' * 10000}])
    base = league.teams[0].roster[0]
    for index in range(12, 50):
        league.teams[0].roster.append(O(**{**vars(base), 'playerId': index, 'name': 'Another roster player ' + str(index),
                                          'stats': {3: {'projected_points': 5}, 2: {'points': 20, 'projected_points': 12}}}))
    events = [injury(), proposal()]
    data = context(league, events)
    assert len(json.dumps(data)) <= 16000
    assert data['event_facts'] == research.normalize_event_facts(league, events)
    assert data['context_omissions']['previous_forecasts'] == 1
    # Even when optional injury detail has gone, the observed status is guarded.
    data['players'] = []
    data['rosters'] = []
    assert 'observed_status_mismatch' in check_transaction_claims('Alpha Receiver is active.', data)
    assert 'pending_trade_as_completed' in check_transaction_claims('Maple acquired Alpha Receiver.', data)


def test_event_normalization_excludes_private_fields_and_rejects_completed_offer(league):
    event = proposal()
    event['memberId'] = 'private-member'
    event['trade']['bidAmount'] = 4321
    event['trade']['comment'] = 'private-comment'
    event['trade']['items'][0]['secret'] = 'secret-value'
    raw = json.dumps(research.normalize_event_facts(league, [event]))
    assert all(value not in raw for value in ('private-member', '4321', 'private-comment', 'secret-value'))
    event['trade']['completed'] = True
    with pytest.raises(ValueError, match='completed transfers'):
        research.normalize_event_facts(league, [event])


def test_private_pending_waivers_are_not_roster_event_context(league):
    event = roster_move()
    event['transaction']['type'] = 'WAIVER'
    event['transaction']['source_status'] = 'PENDING'
    with pytest.raises(ValueError, match='executed roster moves'):
        research.normalize_event_facts(league, [event])


def test_historical_report_cannot_accept_current_event_facts(league):
    with pytest.raises(ValueError, match='historical'):
        research.build_context(league, 'Past week', 'get_league_updates', 1, event_facts=[injury()])


def complete(text):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]}


@pytest.fixture
def inference(monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'test-local-model')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    monkeypatch.setenv('AI_ANALYSIS', 'True')
    monkeypatch.setenv('AI_SECOND_COMMENTATOR', 'True')
    monkeypatch.setenv('AI_RESEARCH_TOOLS', 'False')
    monkeypatch.setattr(analysis, '_archive', Mock())
    from gamedaybot.espn import commentary_quality
    monkeypatch.setattr(commentary_quality, 'check_analysis_value', lambda *args: [])


def test_authoritative_offer_and_managers_reach_both_voices(league, inference, mock_requests):
    first = 'If the offer clears, Alex M. would be exchanging receiver depth for a different roster shape; the fit needs more evidence.'
    second = 'The paperwork is still a proposal. Bobby should demand a football reason before treating the negotiation as a victory parade.'
    mock_requests.post(ENDPOINT, [{'json': complete(first)}, {'json': complete(second)}])
    text = analysis.generate_analysis('New proposed trade between Oak and Maple', 'get_league_updates',
                                      league=league, week=3, event_facts=[proposal()])
    assert first in text and second in text
    assert mock_requests.call_count == 2
    for call in mock_requests.request_history:
        packet = json.loads(call.json()['messages'][1]['content'])
        ctx = packet['research_context']
        assert ctx['observed_event_index']['proposal-one']['completed'] is False
        assert ctx['teams']['1']['managers'] == ['Alex Morgan']
        assert ctx['teams']['2']['observed_events'][0]['items'][0]['direction'] == 'received'
        assert 'not the time an injury occurred' in ctx['event_scope']
    second_packet = json.loads(mock_requests.last_request.json()['messages'][1]['content'])
    assert 'verified_analyst' in second_packet


def test_bad_completed_trade_draft_is_rejected_before_second_voice(league, inference, mock_requests):
    bad = 'Maple acquired Alpha Receiver and should enjoy the new depth.'
    mock_requests.post(ENDPOINT, json=complete(bad))
    text = analysis.generate_analysis('New proposed trade', 'get_league_updates',
                                      league=league, week=3, event_facts=[proposal()])
    assert bad not in text and 'AI Hot Take' not in text
    assert mock_requests.call_count == 2
    assert 'pending_trade_as_completed' in mock_requests.last_request.json()['messages'][-1]['content']


def test_failed_event_context_skips_inference_instead_of_using_report_only(league, inference, mock_requests, monkeypatch):
    monkeypatch.setattr(analysis, 'build_context', Mock(side_effect=ValueError('private-event-details')))
    text = analysis.generate_analysis('Maple proposal', 'get_league_updates',
                                      league=league, week=3, event_facts=[proposal()])
    assert text == '' and mock_requests.call_count == 0


def test_event_keyword_only_forwarded_when_explicitly_supplied(league, inference, mock_requests, monkeypatch):
    builder = Mock(return_value={'players': [], 'rosters': [], 'news': [], 'fantasy_teams': []})
    monkeypatch.setattr(analysis, 'build_context', builder)
    mock_requests.post(ENDPOINT, json=complete('NO_ADDITIONAL_INSIGHT'))
    analysis.generate_analysis('Current standings', 'get_standings', league=league, week=3)
    assert 'event_facts' not in builder.call_args.kwargs
    analysis.generate_analysis('Observed proposal', 'get_league_updates', league=league, week=3, event_facts=[proposal()])
    assert builder.call_args.kwargs['event_facts'] == [proposal()]


def test_league_updates_without_authoritative_events_skip_model(league, inference, mock_requests):
    assert analysis.generate_analysis('League updates', 'get_league_updates', league=league, week=3) == ''
    assert mock_requests.call_count == 0
