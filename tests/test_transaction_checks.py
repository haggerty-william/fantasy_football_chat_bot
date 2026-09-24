from copy import deepcopy

import pytest

from gamedaybot.espn.transaction_checks import check_transaction_claims as check


@pytest.fixture
def context():
    return {'week': 3,
            'fantasy_teams': [{'id': '1', 'name': 'Oak', 'managers': ['Alex Morgan']},
                              {'id': '2', 'name': 'Maple', 'managers': ['Alex Carter']}],
            'players': [{'id': 11, 'name': 'Alpha Receiver'}, {'id': 21, 'name': 'Beta Runner'}],
            'trade_awareness': {'pending': {'trades': [{'state': 'accepted_awaiting_processing', 'completed': False,
                 'items': [{'player_id': '11', 'player_name': 'Alpha Receiver', 'from_team_id': '1', 'to_team_id': '2'},
                           {'player_id': '21', 'player_name': 'Beta Runner', 'from_team_id': '2', 'to_team_id': '1'}]}]},
                'recent_completed': {'trades': []}},
            'schedule_awareness': {'matchups': [{'scoring_weeks': [3], 'team_ids': ['1', '2']},
                                               {'scoring_weeks': [4], 'team_ids': ['1', '2']}]}}


@pytest.mark.parametrize('text', [
    'Maple acquired Alpha Receiver.', 'Oak sent Alpha Receiver to Maple.',
    'Maple has already received Alpha Receiver.', 'Alex C. acquired Alpha Receiver.',
    'Alex Morgan shipped Alpha Receiver to Maple.', 'Alex C (Maple) is thrilled. Maple got Alpha Receiver.',
    'Oak gave up Alpha Receiver for Beta Runner.', 'Maple landed Receiver.',
    'Oak sent Alpha Receiver and Maple received Alpha Receiver.',
])
def test_pending_trade_cannot_be_reported_as_completed(context, text):
    assert 'pending_trade_as_completed' in check(text, context)


@pytest.mark.parametrize('text', [
    'If Maple acquired Alpha Receiver, the depth improves.',
    'Maple would receive Alpha Receiver.',
    'Maple might receive Alpha Receiver if the deal clears.',
    'Maple has not acquired Alpha Receiver.',
    "Maple hasn't acquired Alpha Receiver.",
    'Maple acquired Alpha Receiver in this hypothetical scenario.',
    'The proposed trade: Maple received Alpha Receiver.',
    'Maple will get Alpha Receiver if the pending trade clears.',
    'Alex acquired Alpha Receiver.',  # Shared first name cannot resolve a team.
    'Maple acquired somebody else.',  # No explicit known player.
])
def test_conditional_pending_and_ambiguous_claims_are_not_blocked(context, text):
    assert check(text, context) == []


def test_matching_completed_activity_takes_precedence_over_stale_pending(context):
    completed = deepcopy(context['trade_awareness']['pending']['trades'][0])
    completed.update(completed=True, state='completed')
    context['trade_awareness']['recent_completed']['trades'].append(completed)
    assert check('Maple acquired Alpha Receiver.', context) == []


def test_completed_report_actions_are_also_authoritative(context):
    context['trade_sides'] = [{'team': 'Maple', 'received': ['Alpha Receiver'], 'sent': ['Beta Runner']}]
    assert check('Alex C. acquired Alpha Receiver.', context) == []


def test_different_completed_leg_cannot_validate_pending_exchange(context):
    context['trade_awareness']['recent_completed']['trades'] = [{'completed': True, 'items': [
        {'player_id': '21', 'from_team_id': '2', 'to_team_id': '1'}]}]
    assert 'pending_trade_as_completed' in check('Maple acquired Alpha Receiver.', context)


def test_pending_and_completed_tool_evidence_is_used(context):
    pending = context.pop('trade_awareness')['pending']
    context['tool_evidence'] = [{'tool': 'get_pending_trades', 'result': pending}]
    assert 'pending_trade_as_completed' in check('Maple acquired Alpha Receiver.', context)
    complete = deepcopy(pending)
    complete['trades'][0]['completed'] = True
    context['tool_evidence'].append({'tool': 'get_recent_trades', 'result': complete})
    assert check('Maple acquired Alpha Receiver.', context) == []


def hypothetical(context):
    context.pop('trade_awareness')
    context['tool_evidence'] = [{'tool': 'evaluate_trade_proposal', 'result': {
        'kind': 'hypothetical_trade', 'completed': False, 'teams': [
            {'team_id': '1', 'sent_player_ids': ['11'], 'received_player_ids': ['21']},
            {'team_id': '2', 'sent_player_ids': ['21'], 'received_player_ids': ['11']}]}}]


@pytest.mark.parametrize('text', ['Oak offered Alpha Receiver.', 'Oak proposed trading Alpha Receiver.',
                                  'Maple accepted Alpha Receiver.', 'Alex M. has offered Alpha Receiver.'])
def test_simulation_is_not_proof_an_offer_exists(context, text):
    hypothetical(context)
    assert 'hypothetical_trade_as_offer' in check(text, context)


def test_simulation_is_not_a_completed_trade(context):
    hypothetical(context)
    assert 'hypothetical_trade_as_completed' in check('Maple acquired Alpha Receiver.', context)
    assert check('If Oak offered Alpha Receiver, Maple could improve.', context) == []


def test_actual_offer_supports_offer_claim_even_after_simulation(context):
    pending = context['trade_awareness']['pending']
    hypothetical(context)
    context['tool_evidence'].append({'tool': 'get_pending_trades', 'result': pending})
    assert check('Oak offered Alpha Receiver.', context) == []


@pytest.mark.parametrize('text', ['Oak defeated Maple in Week 4.', 'Maple scored 140 in Week 4.',
                                  'Oak already won its Week 4 matchup.', 'Alex C. lost in Week 4.'])
def test_future_matchup_is_not_a_completed_result(context, text):
    assert 'future_matchup_as_completed' in check(text, context)


@pytest.mark.parametrize('text', ['Oak could defeat Maple in Week 4.', 'Oak beats Maple in Week 4 is my pick.',
                                  'Maple lost in Week 1.', 'Maple scored 140 in Week 3.',
                                  'A Week 4 win would put Oak in first.', 'Maple has not won in Week 4.',
                                  'Oak lost the plot while planning for Week 4.',
                                  'Oak beat the waiver rush before Week 4.',
                                  'Maple won the press conference ahead of Week 4.',
                                  "Oak beat Maple's waiver bid before Week 4."])
def test_future_forecasts_and_past_results_are_allowed(context, text):
    assert check(text, context) == []


def test_explicit_tool_week_state_and_multiweek_mapping(context):
    context.pop('schedule_awareness')
    context['tool_evidence'] = [{'tool': 'get_week_matchups', 'result': {'matchups': [
        {'state': 'future', 'scoring_weeks': [5, 6], 'teams': [{'team_id': '1'}, {'team_id': '2'}]}]}}]
    assert 'future_matchup_as_completed' in check('Oak posted 250 in Week 6.', context)
    assert check('Oak posted 250 in Week 2.', context) == []


def test_compact_full_schedule_period_groups_preserve_future_fact_checks(context):
    context.pop('schedule_awareness')
    context['tool_evidence'] = [{'tool': 'get_fantasy_schedule', 'result': {
        'pairing_columns': ['home_team_id', 'away_team_id', 'home_score', 'away_score',
                            'winner_side', 'playoff_tier', 'pairing_provisional', 'bye'],
        'matchups': [{'matchup_period': 4, 'scoring_weeks': [4], 'state': 'future',
                      'team_ids': ['1', '2'], 'pairings': [['1', '2', None, None, None, 'NONE', False, False]]}]}}]
    assert 'future_matchup_as_completed' in check('Oak defeated Maple in Week 4.', context)
    assert check('Oak could defeat Maple in Week 4.', context) == []
    assert check('Oak defeated Maple in Week 1.', context) == []


def test_matching_verified_complete_week_overrides_future_snapshot(context):
    context['tool_evidence'] = [{'tool': 'get_week_matchups', 'result': {'matchups': [
        {'state': 'completed', 'scoring_weeks': [4], 'teams': [{'team_id': '1'}, {'team_id': '2'}]}]}}]
    assert check('Oak defeated Maple in Week 4.', context) == []


def test_player_initials_do_not_end_assertion(context):
    context['players'][0]['name'] = 'A.J. Receiver'
    context['trade_awareness']['pending']['trades'][0]['items'][0]['player_name'] = 'A.J. Receiver'
    assert 'pending_trade_as_completed' in check('Maple acquired A.J. Receiver.', context)


def test_team_punctuation_does_not_end_assertion(context):
    context['fantasy_teams'][1]['name'] = 'Sports Team!!'
    assert 'pending_trade_as_completed' in check('Sports Team!! acquired Alpha Receiver.', context)


@pytest.mark.parametrize('context', [{}, {'tool_evidence': None}, None])
def test_missing_evidence_does_not_raise_or_invent_a_contradiction(context):
    assert check('Oak acquired Alpha Receiver.', context) == []
