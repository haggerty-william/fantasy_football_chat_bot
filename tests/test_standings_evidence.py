"""Standings commentary gets explanations before requesting optional tools."""
import json
from types import SimpleNamespace as O

import pytest

from gamedaybot.espn import analyst_evidence as evidence, research
from gamedaybot.espn.analysis_packet import AnalysisPacket


def league():
    teams = [O(team_id=i, team_name=name, owners=[], roster=[],
               scores=scores + [9999], outcomes=outcomes + ['U'])
             for i, name, scores, outcomes in (
                 (1, 'Oak', [100, 110], ['W', 'L']),
                 (2, 'Maple', [90, 120], ['L', 'W']),
                 (3, 'Birch', [100, 95], ['W', 'L']),
                 (4, 'Elm', [80, 105], ['L', 'W']))]
    for a, b in ((0, 1), (2, 3)):
        teams[a].schedule = [teams[b]] * 3
        teams[b].schedule = [teams[a]] * 3
    return O(teams=teams, scoringPeriodId=3, settings=O(reg_season_count=14,
             matchup_periods={str(w): [w] for w in range(1, 15)}))


def test_default_standings_and_power_context_explain_completed_results():
    sample = league()
    for kind in ('get_standings', 'get_power_rankings'):
        context = research.build_context(sample, 'Current Standings', kind, 3)
        performance = context['league_history']['teams'][0]['performance']
        assert performance['weeks'] == 2
        assert performance['points_per_game'] == 105
        assert performance['points_per_game_vs_league_average'] == 5
        assert performance['opponent_points_per_game'] == 105
        assert performance['average_margin'] == 0
        assert performance['points_per_game_rank'] == 1
        assert performance['all_play_win_pct'] == 75
        assert performance['actual_win_equivalents'] == 1
        assert performance['expected_win_equivalents'] == 1.5
        assert performance['schedule_luck_wins'] == -.5
        assert performance['recent_games'][1]['margin'] == -10
        assert context['league_history']['performance_context']['through_week'] == 2
        assert '9999' not in json.dumps(context)
        assert len(json.dumps(context)) <= context['context_char_limit']


def test_historical_and_regular_season_cutoffs_exclude_later_scores():
    sample = league()
    history = evidence.history(sample, sample.teams, 1, include_performance=True)
    assert history['teams'][0]['performance']['points_per_game'] == 100
    assert history['teams'][0]['performance']['schedule_luck_wins'] == .17
    sample.settings.reg_season_count = 1
    history = evidence.history(sample, sample.teams, 3, include_performance=True)
    assert history['performance_context']['through_week'] == 1
    assert history['teams'][0]['performance']['points_per_game'] == 100


def test_focused_team_still_compares_against_full_league():
    sample = league()
    history = evidence.history(sample, sample.teams[:1], 3, include_performance=True)
    assert len(history['teams']) == 1
    assert history['teams'][0]['performance']['all_play_win_pct'] == 75
    assert history['teams'][0]['performance']['points_per_game_vs_league_average'] == 5


@pytest.mark.parametrize('missing', [None, float('nan')])
def test_incomplete_week_is_not_zero_or_a_false_scoring_advantage(missing):
    sample = league()
    sample.teams[1].scores[1] = missing
    history = evidence.history(sample, sample.teams, 3, include_performance=True)
    assert history['performance_context']['omitted_weeks'] == [2]
    assert history['teams'][0]['performance']['weeks'] == 1
    assert history['teams'][0]['performance']['points_per_game'] == 100


def test_unverified_multiweek_rules_and_no_samples_do_not_invent_explanations():
    sample = league()
    sample.scoringPeriodId = 1
    history = evidence.history(sample, sample.teams, 1, include_performance=True)
    assert history['teams'][0]['performance'] == {'status': 'No verified completed matchup samples.'}
    sample.settings.matchup_periods = {'1': [1, 2]}
    history = evidence.history(sample, sample.teams, 1, include_performance=True)
    assert 'error' in history['performance_context']
    assert 'performance' not in history['teams'][0]


def test_explanatory_evidence_stays_with_canonical_team_and_names_appear_once():
    sample = league()
    context = research.build_context(sample, 'Oak leads Maple', 'get_standings', 3)
    packet = AnalysisPacket(sample, context, 'Oak leads Maple', 'get_standings', 3, 'now')
    serialized = packet.dumps()
    for team in sample.teams:
        assert serialized.count(team.team_name) == 1
    teams = json.loads(serialized)['research_context']['teams']
    assert teams['1']['history']['performance']['recent_games'][0]['opponent_id'] == '2'
    assert teams['1']['history']['performance']['schedule_luck_wins'] == -.5


def test_opponent_margin_is_distinct_from_league_average_gap():
    sample = league()
    for team, points in zip(sample.teams, [169.24, 149.1, 80, 89.86]):
        team.scores[:2] = [points, points]
    context = research.build_context(sample, 'Current Standings', 'get_standings', 3)
    history = context['league_history']
    performance = history['teams'][0]['performance']
    assert performance['points_per_game'] == 169.24
    assert performance['opponent_points_per_game'] == 149.1
    assert performance['average_margin'] == 20.14
    assert performance['points_per_game_vs_league_average'] == 47.19
    definitions = history['performance_context']['metric_definitions']
    assert 'actual scheduled opponents' in definitions['average_margin']
    assert 'NOT the margin against scheduled opponents' in definitions['points_per_game_vs_league_average']
    packet = AnalysisPacket(sample, context, 'Current Standings', 'get_standings', 3, 'now')
    assert packet.dumps().count('metric_definitions') == 1


def test_cumulative_scoring_rank_is_distinct_from_weekly_rank():
    sample = league()
    history = evidence.history(sample, sample.teams, 3, include_performance=True)
    performance = history['teams'][0]['performance']
    assert performance['points_per_game_rank'] == 1
    weeks = {row['week']: row for row in performance['recent_games']}
    assert weeks[1]['scoring_rank'] == 1
    assert weeks[2]['scoring_rank'] == 2
    definitions = history['performance_context']['metric_definitions']
    assert 'Cumulative' in definitions['points_per_game_rank']
    assert 'explicit week only' in definitions['recent_games.scoring_rank']
