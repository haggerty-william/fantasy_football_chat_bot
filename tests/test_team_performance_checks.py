import pytest

from gamedaybot.espn.team_performance_checks import check_team_performance


CONTEXT = {'fantasy_teams': [{'name': n} for n in ['Oak Owls', 'Maple Meteors', 'Pine Pirates']],
           'league_history': {'teams': [
               {'team': 'Oak Owls', 'performance': {
                   'weeks': 2, 'points_per_game': 169.24, 'opponent_points_per_game': 149.1,
                   'average_margin': 20.14, 'points_per_game_vs_league_average': 47.19,
                   'points_per_game_rank': 1,
                   'recent_games': [{'week': 1, 'scoring_rank': 2}, {'week': 2, 'scoring_rank': 1}]}},
               {'team': 'Maple Meteors', 'performance': {
                   'weeks': 2, 'points_per_game': 110, 'opponent_points_per_game': 140,
                   'average_margin': -30, 'points_per_game_vs_league_average': -12.05,
                   'points_per_game_rank': 5,
                   'recent_games': [{'week': 1, 'scoring_rank': 7}, {'week': 2, 'scoring_rank': 3}]}},
           ]}}


def check(text, context=CONTEXT):
    return check_team_performance(text, context)


def test_live_error_league_average_gap_is_not_opponent_margin():
    assert check('Oak Owls leads the table. They have outscored opponents by 47.19 points per game.') == [
        'opponent_average_margin_mismatch']


@pytest.mark.parametrize('text', [
    'Oak Owls has outscored opponents by 20.14 points per game.',
    'Oak Owls has an average margin of 20.14 points.',
    'Oak Owls has scored 47.19 points per game above the league average.',
    'Maple Meteors sits 12.05 points per game below the league average.',
])
def test_correct_metric_claims_pass(text):
    assert check(text) == []


def test_direct_mean_margin_and_league_gap_contradictions():
    assert check('Oak Owls has an average margin of 47.19 points.') == ['opponent_average_margin_mismatch']
    assert check('Oak Owls scores 20.14 points per game above the league average.') == ['league_average_gap_mismatch']
    assert check('Maple Meteors sits 12.05 points above the league average.') == ['league_average_gap_mismatch']


def test_single_week_gap_is_not_compared_to_multiweek_average():
    assert check('Oak Owls scored 30 points above the league average in week 1.') == []
    assert check('Oak Owls scored 30 points per game above the league average through week 2.') == [
        'league_average_gap_mismatch']


def test_live_error_sample_rank_is_not_weekly_rank():
    assert check('Maple Meteors has a respectable scoring rank of 5th in Week 1.') == ['weekly_scoring_rank_mismatch']


def test_invented_scoring_rank_tie_cannot_borrow_a_different_teams_rank():
    assert check('Oak Owls leads. While they are technically tied for the top spot in scoring rank '
                 'with Maple Meteors, the reality is different.') == ['scoring_rank_tie_mismatch']
    assert check('Oak Owls and Maple Meteors are tied for the top spot in scoring.') == ['scoring_rank_tie_mismatch']
    assert check('Oak Owls and Maple Meteors are not tied for the top spot in scoring.') == []


def test_actual_scoring_tie_and_record_ties_are_distinct():
    from copy import deepcopy
    context = deepcopy(CONTEXT)
    context['league_history']['teams'][1]['performance']['points_per_game_rank'] = 1
    assert check('Oak Owls and Maple Meteors are tied for the top spot in scoring.', context) == []
    assert check('Oak Owls and Maple Meteors are tied in the standings, but their scoring differs.') == []


@pytest.mark.parametrize('text', [
    'Maple Meteors has a scoring rank of 7th in week 1.',
    'Maple Meteors ranks 3rd in scoring in week 2.',
    'Maple Meteors has a scoring rank of 5th through week 2.',
    'Maple Meteors ranks 5th in points per game.',
])
def test_correct_weekly_and_sample_ranks_pass(text):
    assert check(text) == []


def test_sample_scoring_rank_has_its_own_check():
    assert check('Maple Meteors ranks 7th in scoring.') == ['sample_scoring_rank_mismatch']


def test_seed_is_not_assumed_to_be_a_scoring_rank():
    assert check('Maple Meteors ranks 7th in the standings.') == []


def test_weekly_rank_for_missing_week_is_not_guessed_from_sample_rank():
    assert check('Maple Meteors has a scoring rank of 5th in week 9.') == []


def test_pronoun_subject_retained_only_in_unambiguous_short_paragraph():
    assert check('Maple Meteors deserves some sympathy. Their scoring rank of 5th in week 1 tells the tale.') == [
        'weekly_scoring_rank_mismatch']
    assert check('Oak Owls and Maple Meteors lead the discussion. They outscored opponents by 47.19 points per game.') == []
    assert check('Oak Owls leads.\n\nThey outscored opponents by 47.19 points per game.') == []
    assert check('Oak Owls leads. Pine Pirates follows. They outscored opponents by 47.19 points per game.') == []
    assert check('Oak Owls leads. Player Alpha changed the result. They outscored opponents by 47.19 points per game.') == []


def test_punctuated_team_name_does_not_break_pronoun_binding():
    context = {'league_history': {'teams': [{'team': 'Sports Team!!', 'performance': {'average_margin': 20.14}}]}}
    assert check('Sports Team!! is thriving. They outscored opponents by 47.19 points per game.', context) == [
        'opponent_average_margin_mismatch']


@pytest.mark.parametrize('text', [
    'The losses will keep coming unless Tanner can stabilize the defense.',
    'Alex needs to reduce opponent scoring.',
    'Maple Meteors must tighten up their defense.',
    'Oak Owls should play better defense.',
])
def test_fantasy_managers_cannot_control_opponents_scores(text):
    assert check(text) == ['unsupported_opponent_scoring_control']


@pytest.mark.parametrize('text', [
    'Tanner cannot play defense against the fantasy opponent.',
    "Alex can't stabilize the defense in a fantasy league.",
    'Opponent scoring is outside the manager\'s control.',
    'Alex can stream another D/ST to improve the defense slot.',
    'Starting a replacement D/ST may improve the defense.',
    'Maple Meteors drew the wrong opponents; the schedule did the mugging.',
])
def test_negation_actual_dst_moves_and_schedule_humor_remain_valid(text):
    assert check(text) == []


@pytest.mark.parametrize('context', [None, {}, {'league_history': {'teams': []}}])
def test_unavailable_performance_does_not_invent_expected_values(context):
    assert check('Oak Owls outscored opponents by 47.19 points per game.', context) == []


@pytest.mark.parametrize('text', [
    "The Analyst is hailing Joe Example as a gold standard, but a two-week sprint isn't a marathon. "
    'This juggernaut status is built on a tiny sample size that could evaporate the moment they face a defense '
    'capable of actually stifling their production.',
    'Oak Owls will face a defense that can shut down their scoring.',
    'They are facing tougher defenses capable of limiting their production.',
])
def test_fantasy_schedule_does_not_supply_a_defense_that_suppresses_the_team(text):
    assert check(text)==['unsupported_opponent_scoring_control']


@pytest.mark.parametrize('text', [
    'Fantasy teams do not face a defense that can stifle their production.',
    'Oak Owls will face a team capable of outscoring them.',
    'Oak Owls may face opponents who score more points; their production could regress.',
])
def test_legitimate_schedule_risk_and_negation_survive_new_defense_guard(text):
    assert check(text)==[]


def test_real_nfl_player_can_face_a_defense_that_affects_his_production():
    from copy import deepcopy
    context=deepcopy(CONTEXT)
    context['players']=[{'id':'7','name':'Player Alpha'}]
    assert check('Player Alpha will face a defense capable of stifling his production.',context)==[]
    context['players']=[]
    context['tool_evidence']=[{'tool':'search_players','result':{'players':[{'id':'7','name':'Player Alpha'}]}}]
    assert check('Player Alpha will face a defense capable of stifling his production.',context)==[]


def test_verified_nfl_team_comments_remain_separate_from_fantasy_management():
    from copy import deepcopy
    from gamedaybot.espn.commentary_checks import check_commentary
    context=deepcopy(CONTEXT)
    context['tool_evidence']=[{'tool':'get_nfl_scoreboard','result':{'games':[{'teams':[
        {'nfl_team':'BUF','name':'Buffalo Bills'}]}]}}]
    text='Buffalo Bills will face a defense capable of stifling their production.'
    assert 'unsupported_opponent_scoring_control' not in check_commentary(text,'',context)
