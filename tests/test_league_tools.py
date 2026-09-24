from copy import deepcopy
import json
import time
from types import SimpleNamespace as O
from unittest.mock import Mock

import pytest

from gamedaybot.espn import league_tools as tools


def matchup(period, score_a=100, score_b=90, winner='HOME', playoff='NONE'):
    return {'matchupPeriodId': period, 'winner': winner, 'playoffTierType': playoff,
            'home': {'teamId': 1, 'totalPoints': score_a}, 'away': {'teamId': 2, 'totalPoints': score_b}}


@pytest.fixture
def evidence(monkeypatch):
    periods = {str(w): [w] for w in range(1, 19)}
    teams = [O(team_id=1, team_name='Oak', roster=[O(playerId=7, name='Runner One', position='RB',
               proTeam='BUF', lineupSlot='RB', injuryStatus='QUESTIONABLE', acquisitionType='DRAFT')]),
             O(team_id=2, team_name='Pine', roster=[])]
    league = O(teams=teams, year=2026, league_id=123, scoringPeriodId=3, current_week=3,
               settings=O(matchup_periods=periods), player_map={7: 'Runner One', 8: 'Receiver Two'},
               espn_request=O(cookies={'espn_s2': 'test-cookie-never-output'}))
    researcher = O(league=league, context={}, week=3, deadline=time.monotonic() + 120)
    data = {'members': [{'email': 'private@example.test', 'id': 'private-member-id'}],
            'settings': {
                'scheduleSettings': {'matchupPeriods': periods, 'playoffTeamCount': 4,
                                     'matchupPeriodCount': 14, 'divisions': [{'id': 0, 'name': 'North'}]},
                'scoringSettings': {'scoringType': 'H2H_POINTS', 'matchupTieRule': 'NONE',
                                    'scoringItems': [{'statId': 53, 'points': 1, 'pointsOverrides': {'16': 2}}]},
                'rosterSettings': {'lineupSlotCounts': {'0': 1, '2': 2, '20': 5}, 'lineupLocktimeType': 'INDIVIDUAL_GAME'},
                'acquisitionSettings': {'isUsingAcquisitionBudget': True, 'acquisitionBudget': 100, 'waiverHours': 1},
                'tradeSettings': {'deadlineDate': 1794000000000, 'vetoVotesRequired': 4, 'privateNote': 'hidden'},
                'draftSettings': {'keeperCount': 2, 'type': 'SNAKE'}, 'password': 'secret-password'},
            'teams': [{'id': 1, 'divisionId': 0, 'playoffSeed': 1, 'waiverRank': 3,
                       'owners': ['private-member-id'], 'record': {'overall': {'wins': 2, 'losses': 0, 'ties': 0, 'pointsFor': 230},
                                                                  'division': {'wins': 1, 'losses': 0}},
                       'transactionCounter': {'acquisitions': 3, 'acquisitionBudgetSpent': 12, 'drops': 3, 'trades': 1}},
                      {'id': 2, 'divisionId': 0}],
            'schedule': [matchup(1), matchup(2, 130, 125)] + [matchup(w, 0, 0, 'UNDECIDED', 'WINNERS_BRACKET' if w >= 15 else 'NONE') for w in range(3, 19)],
            'draftDetail': {'drafted': True, 'picks': [
                {'teamId': 1, 'playerId': 7, 'roundId': 1, 'roundPickNumber': 1, 'bidAmount': 15, 'keeper': True},
                {'teamId': 2, 'playerId': 8, 'roundId': 1, 'roundPickNumber': 2, 'bidAmount': 0, 'keeper': False}]}}
    read = Mock(return_value=data)
    monkeypatch.setattr(tools, 'read_league', read)
    return researcher, data, read


def test_full_future_schedule_has_period_18_and_no_future_zero_results(evidence):
    r, _, read = evidence
    result = tools.execute('get_fantasy_schedule', {}, r)
    assert len(result['matchups']) == 16
    last = result['matchups'][-1]
    assert last['matchup_period'] == 18
    pair = dict(zip(result['pairing_columns'], last['pairings'][0]))
    assert pair['home_score'] is None and 'winner_side' not in pair
    assert pair['pairing_provisional'] is True
    assert result['matchups'][0]['state'] == 'in_progress'
    read.assert_called_once()


def test_explicit_period_scores_and_bye_are_preserved(evidence):
    r, data, _ = evidence
    data['schedule'][0].pop('away')
    result = tools.execute('get_week_matchups', {'matchup_period': 1, 'team_id': '1'}, r)
    row = result['matchups'][0]
    assert row['bye'] is True and row['teams'][0]['score'] == 100
    assert row['state'] == 'completed'
    assert tools.execute('get_week_matchups', {'matchup_period': 1, 'team_id': '2'}, r)['matchups'] == []


def test_multiweek_period_not_falsely_completed_or_treated_as_week(evidence):
    r, data, _ = evidence
    data['settings']['scheduleSettings']['matchupPeriods'] = {'1': [1, 2], '2': [3, 4], '3': [5, 6]}
    r.league.settings.matchup_periods = data['settings']['scheduleSettings']['matchupPeriods']
    data['schedule'] = [matchup(1, 230, 215), matchup(2, 10, 15), matchup(3, 0, 0)]
    result = tools.execute('get_week_matchups', {'matchup_period': 2}, r)
    assert result['matchups'][0]['state'] == 'in_progress'
    assert result['matchups'][0]['scoring_weeks'] == [3, 4]
    splits = tools.execute('get_scoring_splits', {'team_id': '1'}, r)['teams'][0]
    assert splits['covered_matchups'] == 1 and splits['mean_points_per_matchup'] == 230


def test_schedule_filter_unknown_period_and_missing_schedule(evidence):
    r, data, _ = evidence
    data['settings']['scheduleSettings']['matchupPeriods'] = {'1': [1]}
    assert 'error' in tools.execute('get_week_matchups', {'matchup_period': 2}, r)
    data.pop('schedule')
    assert 'error' in tools.execute('get_fantasy_schedule', {}, r)


def test_rules_preserve_verified_slots_overrides_and_omit_private_fields(evidence):
    r, _, _ = evidence
    result = tools.execute('get_league_rules', {}, r)
    assert result['lineup_slots'] == {'QB': 1, 'RB': 2, 'BE': 5}
    assert result['scoring'][0]['position_overrides'] == {'16': 2}
    assert result['trades']['deadlineDate'] == 1794000000000
    assert result['draft']['keeperCount'] == 2
    assert result['waivers']['isUsingAcquisitionBudget'] is True
    raw = json.dumps(result)
    assert 'secret-password' not in raw and 'privateNote' not in raw and 'private-member-id' not in raw


def test_profile_has_faab_counters_and_roster_without_owner_details(evidence):
    r, _, _ = evidence
    result = tools.execute('get_team_profile', {'team_id': '1'}, r)
    assert result['faab_remaining_estimate'] == 88
    assert result['transaction_counts']['trades'] == 1
    assert result['records']['division']['wins'] == 1
    assert result['roster'][0]['lineup_slot'] == 'RB'
    assert result['roster'][0]['id'] == '7'
    assert result['division']['name'] == 'North'
    assert 'private-member-id' not in json.dumps(result)
    assert 'private@example.test' not in json.dumps(result)


def test_profile_missing_spending_or_non_faab_is_unknown_not_zero(evidence):
    r, data, _ = evidence
    data['teams'][0]['transactionCounter'].pop('acquisitionBudgetSpent')
    assert tools.execute('get_team_profile', {'team_id': '1'}, r)['faab_remaining_estimate'] is None
    data['settings']['acquisitionSettings']['isUsingAcquisitionBudget'] = False
    result = tools.execute('get_team_profile', {'team_id': '1'}, r)
    assert result['faab_initial_budget'] is None and result['faab_remaining_estimate'] is None
    assert result['faab_enabled'] is False


def test_scoring_splits_exclude_current_scores_and_missing_observations(evidence):
    r, data, _ = evidence
    data['schedule'][2]['home']['totalPoints'] = 999
    result = tools.execute('get_scoring_splits', {'team_id': '1'}, r)['teams'][0]
    assert result['covered_matchups'] == 2
    assert result['mean_points_per_matchup'] == result['median_points_per_matchup'] == 115
    assert result['points_population_stddev'] == 15
    assert result['mean_opponent_points'] == 107.5
    data['schedule'][1]['away']['totalPoints'] = None
    result = tools.execute('get_scoring_splits', {'team_id': '1'}, r)['teams'][0]
    assert result['covered_matchups'] == 1 and result['omitted_matchup_periods'] == [2]
    assert result['points_population_stddev'] is None


def test_head_to_head_uses_official_winner_not_score_comparison(evidence):
    r, data, _ = evidence
    data['schedule'][1]['winner'] = 'AWAY'
    result = tools.execute('get_head_to_head', {'team_id': '1', 'opponent_team_id': '2'}, r)
    assert result['completed_record'] == {'wins': 1, 'losses': 1, 'ties': 0}
    assert len(result['matchups']) == 18
    data['schedule'][0]['winner'] = 'TIE'
    assert tools.execute('get_head_to_head', {'team_id': '2', 'opponent_team_id': '1'}, r)['completed_record'] == {'wins': 1, 'losses': 0, 'ties': 1}


def test_draft_board_paginates_keeps_draft_owner_and_unknown_metadata(evidence):
    r, data, _ = evidence
    first = tools.execute('get_draft_board', {'limit': 1}, r)
    assert first['next_offset'] == 1 and first['total_matching_picks'] == 2
    pick = first['teams'][0]['picks'][0]
    assert first['teams'][0]['team_id'] == '1' and pick['bid_amount'] == 15 and pick['keeper'] is True
    second = tools.execute('get_draft_board', {'offset': first['next_offset'], 'limit': 1}, r)
    assert second['teams'][0]['team_id'] == '2' and second['next_offset'] is None
    data['draftDetail']['picks'][1].pop('keeper')
    selected = tools.execute('get_draft_board', {'team_id': '2'}, r)
    assert selected['total_matching_picks'] == 1 and selected['teams'][0]['picks'][0]['keeper'] is None


def box_entry(pid=7, actual=11, projected=9):
    stats = []
    if actual is not None:
        stats.append({'seasonId': 2026, 'scoringPeriodId': 1, 'statSourceId': 0, 'statSplitTypeId': 1,
                      'appliedTotal': actual, 'appliedStats': {'53': actual}})
    if projected is not None:
        stats.append({'seasonId': 2026, 'scoringPeriodId': 1, 'statSourceId': 1, 'appliedTotal': projected})
    stats.append({'seasonId': 2025, 'scoringPeriodId': 1, 'statSourceId': 0, 'appliedTotal': 999})
    return {'lineupSlotId': 2, 'playerPoolEntry': {'player': {'id': pid, 'fullName': 'Runner One',
            'defaultPositionId': 2, 'injuryStatus': 'OUT', 'stats': stats}}}


def test_box_score_filters_period_and_separates_week_stats_and_projections(evidence):
    r, data, read = evidence
    match = data['schedule'][0]
    match['home']['rosterForCurrentScoringPeriod'] = {'entries': [box_entry()]}
    match['away']['rosterForCurrentScoringPeriod'] = {'entries': [box_entry(8, None, None)]}
    result = tools.execute('get_week_box_scores', {'team_id': '1', 'week': 1}, r)
    home, away = result['teams']
    assert home['players'][0]['points'] == 11 and home['players'][0]['projected_points'] == 9
    assert away['players'][0]['points'] is None and away['players'][0]['projected_points'] is None
    assert 'injuryStatus' not in json.dumps(result) and 'OUT' not in json.dumps(result)
    kwargs = read.call_args.kwargs
    assert kwargs['params'] == {'scoringPeriodId': 1}
    assert kwargs['filters'] == {'schedule': {'filterMatchupPeriodIds': {'value': [1]}}}


def test_box_missing_roster_returns_unavailable_instead_of_current_roster(evidence):
    r, _, _ = evidence
    assert 'error' in tools.execute('get_week_box_scores', {'team_id': '1', 'week': 1}, r)


def test_box_rejects_future_without_fetching(evidence):
    r, _, read = evidence
    assert 'error' in tools.execute('get_week_box_scores', {'team_id': '1', 'week': 4}, r)
    read.assert_not_called()


@pytest.mark.parametrize('name,args', [
    ('get_team_profile', {'team_id': 1}), ('get_team_profile', {'team_id': '999'}),
    ('get_team_profile', {'team_id': '1', 'url': 'https://example.test'}),
    ('get_fantasy_schedule', {'year': 2020}), ('get_league_rules', []),
    ('get_week_matchups', {'matchup_period': True}), ('get_week_matchups', {'matchup_period': 19}),
    ('get_week_box_scores', {'team_id': '1', 'week': 1.0}),
    ('get_draft_board', {'offset': -1}), ('get_draft_board', {'limit': 61}),
    ('get_head_to_head', {'team_id': '1', 'opponent_team_id': '1'}), ('unknown', {}),
])
def test_invalid_arguments_never_fetch(evidence, name, args):
    r, _, read = evidence
    assert 'error' in tools.execute(name, args, r)
    read.assert_not_called()


@pytest.mark.parametrize('name,args', [('get_team_profile', {'team_id': '1'}),
                                    ('get_fantasy_schedule', {}), ('get_league_rules', {})])
def test_historical_reports_block_current_sensitive_context(evidence, name, args):
    r, _, read = evidence
    r.context['historical'] = True
    assert 'error' in tools.execute(name, args, r)
    read.assert_not_called()


def test_historical_head_to_head_excludes_later_scores_and_playoff_pairings(evidence):
    r, _, _ = evidence
    r.context['historical'] = True
    r.week = 1
    result = tools.execute('get_head_to_head', {'team_id': '1', 'opponent_team_id': '2'}, r)
    assert len(result['matchups']) == 1 and result['completed_record']['wins'] == 1
    assert tools.execute('get_week_matchups', {'matchup_period': 2}, r)['matchups'] == []


def test_shared_cache_is_request_scoped_and_respects_expired_deadline(evidence):
    r, _, read = evidence
    tools.execute('get_league_rules', {}, r)
    tools.execute('get_team_profile', {'team_id': '1'}, r)
    tools.execute('get_fantasy_schedule', {}, r)
    assert read.call_count == 1
    new_request = O(league=r.league, context={}, week=3, deadline=r.deadline)
    tools.execute('get_fantasy_schedule', {}, new_request)
    assert read.call_count == 2
    r.deadline = time.monotonic()
    assert 'error' in tools.execute('get_fantasy_schedule', {}, r)
    assert read.call_count == 2


def test_source_failure_never_exposes_exception_details(evidence):
    r, _, read = evidence
    read.side_effect = ValueError('Cookie espn_s2=private-data, private-member-id')
    result = tools.execute('get_league_rules', {}, r)
    assert 'error' in result
    assert 'private-data' not in json.dumps(result) and 'private-member-id' not in json.dumps(result)


def test_evidence_read_only_and_strict_function_schemas(evidence):
    r, data, _ = evidence
    original = deepcopy(data)
    for name, args in [('get_fantasy_schedule', {}), ('get_team_profile', {'team_id': '1'}),
                       ('get_scoring_splits', {}), ('get_draft_board', {}), ('get_league_rules', {})]:
        tools.execute(name, args, r)
    assert data == original
    assert len(tools.NAMES) == 8
    assert all(t['function']['parameters']['additionalProperties'] is False for t in tools.TOOLS)


def test_default_schedule_awareness_is_compact_deduplicated_without_network(evidence):
    r, _, read = evidence
    a, b = r.league.teams
    a.schedule, b.schedule = [b] * 18, [a] * 18
    r.league.settings.reg_season_count = 14
    result = tools.build_schedule_awareness(r.league, 13)
    assert [m['matchup_period'] for m in result['matchups']] == [13, 14, 15]
    assert all(m['team_ids'] == ['1', '2'] for m in result['matchups'])
    assert result['matchups'][2]['pairing_provisional'] is True
    assert result['matchups'][0]['pairing_provisional'] is False
    assert 'Oak' not in json.dumps(result) and 'Pine' not in json.dumps(result)
    read.assert_not_called()


def test_default_schedule_awareness_refuses_unverified_periods_and_pairings(evidence):
    r, _, _ = evidence
    a, b = r.league.teams
    a.schedule, b.schedule = [b] * 18, []
    result = tools.build_schedule_awareness(r.league, 3)
    assert result['matchups'] == [] and result['unavailable']
    r.league.settings.matchup_periods = {'1': [1, 2], '2': [3, 4]}
    assert 'unavailable' in tools.build_schedule_awareness(r.league, 3)['status']
    assert tools.build_schedule_awareness(r.league, True)['matchups'] == []


def test_default_schedule_confirmed_self_opponent_is_bye(evidence):
    r, _, _ = evidence
    a, b = r.league.teams
    a.schedule, b.schedule = [a] * 18, [b] * 18
    result = tools.build_schedule_awareness(r.league, 18)
    assert len(result['matchups']) == 2 and all(m['bye'] for m in result['matchups'])


@pytest.mark.parametrize('team_count', [10, 20])
def test_full_league_schedule_stays_below_14k_and_keeps_every_pair(evidence, team_count):
    r, data, _ = evidence
    r.week = r.league.scoringPeriodId = r.league.current_week = 1
    r.league.teams = [O(team_id=i, team_name=f'Long Fantasy Team Name {i}', roster=[]) for i in range(1, team_count + 1)]
    data['schedule'] = []
    for period in range(1, 19):
        for first in range(1, team_count + 1, 2):
            row = matchup(period, 123.45, 67.89, 'UNDECIDED', 'WINNERS_BRACKET' if period > 14 else 'NONE')
            row['home']['teamId'], row['away']['teamId'] = first, first + 1
            data['schedule'].append(row)
    result = tools.execute('get_fantasy_schedule', {}, r)
    assert result['returned_matchup_count'] == 18 * team_count // 2
    assert result['omitted_matchups'] == 0
    assert len(json.dumps(result, ensure_ascii=False)) < 14000
    current = dict(zip(result['pairing_columns'], result['matchups'][0]['pairings'][0]))
    assert current['home_score'] == 123.45 and current['away_score'] == 67.89
    for period in result['matchups']:
        assert len(period['pairings']) == team_count // 2
        assert set(period['team_ids']) == {str(i) for i in range(1, team_count + 1)}


@pytest.mark.parametrize('nonzero', [False, True])
def test_real_sized_box_scores_fit_14k_without_losing_player_totals(evidence, nonzero):
    r, data, _ = evidence
    for side in ('home', 'away'):
        entries = []
        for i in range(18):
            entry = box_entry(100 + i + (18 if side == 'away' else 0), 12.25 + i, 10.55 + i)
            raw = entry['playerPoolEntry']['player']
            raw['fullName'] = f'Long Named Fantasy Player {i}'
            raw['stats'][0]['appliedStats'] = {str(k): k / 10 if nonzero else 0 for k in range(40)}
            entries.append(entry)
        data['schedule'][0][side]['rosterForCurrentScoringPeriod'] = {'entries': entries}
    result = tools.execute('get_week_box_scores', {'team_id': '1', 'week': 1}, r)
    assert 'error' not in result
    assert result['returned_player_count'] == 36 and result['omitted_player_count'] == 0
    assert len(json.dumps(result, ensure_ascii=False)) < 14000
    for team in result['teams']:
        assert len(team['players']) == 18
        for i, player in enumerate(team['players']):
            assert player['points'] == 12.25 + i and player['projected_points'] == 10.55 + i
            assert len(player['scoring_contributions']) <= 6
            assert all(stat['points'] != 0 for stat in player['scoring_contributions'])
            assert player['omitted_nonzero_contributions'] + len(player['scoring_contributions']) == (39 if nonzero else 0)
    assert result['omitted_scoring_contribution_count'] == sum(p['omitted_nonzero_contributions'] for t in result['teams'] for p in t['players'])
