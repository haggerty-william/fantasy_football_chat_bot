"""Editorial regression examples: analysis should add more than table prose."""

import pytest

from gamedaybot.espn.commentary_quality import check_analysis_value


STANDINGS = '''League standings
1. Oak Owls (2-0) PF 260 PA 180
2. Maple Meteors (2-0) PF 245 PA 230
3. Cedar Cats (1-1) PF 250 PA 251
4. Pine Pirates (1-1) PF 215 PA 200
5. Birch Bears (0-2) PF 270 PA 290
6. Elm Eagles (0-2) PF 180 PA 240'''
CONTEXT = {'fantasy_teams': [{'name': name} for name in (
    'Oak Owls', 'Maple Meteors', 'Cedar Cats', 'Pine Pirates', 'Birch Bears', 'Elm Eagles'
)]}


def check(text, report=STANDINGS, report_type='get_standings', context=CONTEXT):
    return check_analysis_value(text, report, report_type, context)


def test_standings_paraphrase_and_empty_optimism_are_rejected():
    text = '''Oak Owls and Maple Meteors are currently leading the league with perfect 2-0 records.
Oak Owls holds the top seed, while Maple Meteors sits at number two.
Both teams are looking to maintain their momentum as they head into the next round of matchups.
The middle of the pack is crowded with two teams tied at 1-1.
Cedar Cats and Pine Pirates are all fighting for positioning.
Meanwhile, Pine Pirates sits at 1-1 but is currently outside the mathematical bounds for the playoffs.
Birch Bears and Elm Eagles are struggling at the bottom of the standings with 0-2 records.
They face a difficult path to recovery, as they currently sit two games back from the leaders.'''
    assert check(text) == ['report_restatement_without_analysis', 'generic_analysis_filler']


def test_table_narration_without_boilerplate_is_rejected():
    assert check('Oak Owls holds the top seed at 2-0. Maple Meteors has a perfect 2-0 record.') == [
        'report_restatement_without_analysis'
    ]


def test_generic_filler_without_a_table_is_rejected():
    assert check('Every week counts. Only time will tell.') == ['generic_analysis_filler']


@pytest.mark.parametrize('text', [
    'Birch Bears are winless despite scoring more than either unbeaten team. The schedule has been collecting rent.',
    'Oak Owls has benefited from schedule luck: the all-play comparison is much less flattering than the record.',
    'Birch Bears face the highest points against; that explains more about the losses than calling the roster weak.',
    'Alex left 20 points on the bench, enough to cover the 10-point loss. Hindsight gets a perfect lineup every week.',
    "The snapshot cannot explain the standings: completed opponent scores are missing. No fake diagnosis today.",
    'Oak Owls holds the top seed. Maple Meteors has a perfect record. Despite that tie, the scoring gap suggests different levels of production.',
    # No required connector, buzzword, joke or minimum number of sentences.
    'The record flatters Oak Owls; that soft schedule deserves a thank-you card.',
])
def test_interpretation_can_reference_standings_without_being_rejected(text):
    assert check(text) == []


@pytest.mark.parametrize('report_type', ['get_final', 'get_scoreboard_short', 'get_projected_scoreboard'])
def test_multiple_score_sentences_add_nothing_to_existing_board(report_type):
    assert check('Oak Owls scored 100 points. Maple Meteors scored 90 points.',
                 'Oak Owls 100 - 90 Maple Meteors', report_type) == ['report_restatement_without_analysis']


def test_concise_projection_contrast_remains_valid():
    assert check('Oak leads, but Maple has the higher projection.',
                 'OAK 100 - 90 MAP\nProjected: OAK 110 - 115 MAP', 'get_scoreboard_short', None) == []


def test_concise_trade_verdict_is_not_forced_to_restate_the_deal():
    assert check('I favor Alex: the deal fixes the empty RB slot while leaving enough WR depth.',
                 'Oak Owls received Player Alpha; Maple Meteors received Player Beta.',
                 'get_trade_report') == []


def test_table_numbers_can_be_reused_to_explain_a_loss():
    assert check('Oak Owls lost 100-110 because the starting WR slot contributed zero; '
                 'the bench alternative scored 20. Alex gets the hindsight trophy, not proof of a bad pregame call.',
                 'Oak Owls 100 - 110 Maple Meteors\nWR 0\nBench 20', 'get_final') == []


def test_commentary_with_no_matching_report_is_not_guessed_redundant():
    assert check('Oak Owls holds the top seed at 2-0. Maple Meteors has a perfect 2-0 record.',
                 'Report temporarily unavailable', context=None) == []


def test_context_can_be_canonical_team_map():
    assert check('Oak Owls holds the top seed. Maple Meteors sits at number two.',
                 '[team:1]\n[team:2]',
                 context={'teams': {'1': {'name': 'Oak Owls'}, '2': {'name': 'Maple Meteors'}}}) == [
        'report_restatement_without_analysis'
    ]


def test_near_verbatim_report_rows_are_rejected_even_without_standings_phrases():
    report = 'Oak Owls 100 points 12 projected remaining\nMaple Meteors 90 points 30 projected remaining'
    assert check(report, report, 'get_scoreboard_short') == ['report_restatement_without_analysis']
