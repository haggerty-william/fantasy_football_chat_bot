"""Analysis should add a supported insight after the visible report."""
import json
from types import SimpleNamespace

import pytest

from gamedaybot.espn import analysis
from gamedaybot.espn.commentary_checks import check_commentary


ENDPOINT = 'http://localhost:1234/v1/chat/completions'
REPORT = ('League standings\n1: (2-0) Oak\n2: (2-0) Maple\n'
          '3: (1-1) Birch\n4: (0-2) Pine')
RECAP = ('Oak and Maple are leading the league with perfect 2-0 records. '
         'Oak holds the top seed, while Maple sits at number two. '
         'Both teams are looking to maintain their momentum. '
         'Birch is fighting for positioning at 1-1. '
         'Pine is struggling at the bottom of the standings with a 0-2 record.')
INSIGHT = ("Oak's scoring supports its record, but Maple's all-play results suggest "
           'the schedule has been kinder than the table admits. '
           'Another soft opponent is a shaky business plan, not a repeatable scoring advantage.')


def response(text):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}


@pytest.fixture
def setup(monkeypatch):
    teams = [SimpleNamespace(team_id=i, team_name=n, owners=[])
             for i, n in enumerate(('Oak', 'Maple', 'Birch', 'Pine'), 1)]
    league = SimpleNamespace(teams=teams)
    context = {'players': [], 'news': [], 'rosters': [], 'fantasy_teams': [
        {'id': str(t.team_id), 'name': t.team_name, 'managers': []} for t in teams],
        'league_history': {'teams': [
            {'team': 'Oak', 'performance': {'scoring_rank': 1, 'all_play_win_pct': 100}},
            {'team': 'Maple', 'performance': {'scoring_rank': 3, 'all_play_win_pct': 50}},
        ]}}
    monkeypatch.setenv('AI_MODEL', 'local-test-model')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    monkeypatch.setenv('AI_ANALYSIS', 'True')
    monkeypatch.setattr(analysis, 'build_context', lambda *a, **kw: context)
    monkeypatch.setattr(analysis, '_archive', lambda *a: None)
    return league, context


def test_standings_paraphrase_is_rewritten_with_same_evidence(setup, mock_requests):
    league, _ = setup
    mock_requests.post(ENDPOINT, [{'json': response(RECAP)}, {'json': response(INSIGHT)}])
    result = analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league)
    assert result.endswith(INSIGHT)
    assert 'maintain their momentum' not in result
    assert mock_requests.call_count == 2
    first, repair = [r.json() for r in mock_requests.request_history]
    assert first['messages'][1] == repair['messages'][1]
    assert repair['messages'][-1]['role'] == 'user'
    assert 'report_restatement_without_analysis' in repair['messages'][-1]['content']
    packet = json.loads(first['messages'][1]['content'])
    assert packet['research_context']['teams']['2']['history']['performance']['all_play_win_pct'] == 50


def test_failed_editorial_rewrite_does_not_append_another_data_recap(setup, mock_requests):
    league, _ = setup
    mock_requests.post(ENDPOINT, json=response(RECAP))
    assert analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league) == ''
    assert mock_requests.call_count == 2


@pytest.mark.parametrize('after_repair', [False, True])
def test_no_extra_insight_sentinel_is_never_published(setup, mock_requests, after_repair):
    league, _ = setup
    replies = ([{'json': response(RECAP)}] if after_repair else [])
    mock_requests.post(ENDPOINT, replies + [{'json': response(analysis.NO_INSIGHT)}])
    assert analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league) == ''
    assert mock_requests.call_count == (2 if after_repair else 1)


def test_evidence_based_wit_does_not_require_another_request(setup, mock_requests):
    league, _ = setup
    mock_requests.post(ENDPOINT, json=response(INSIGHT))
    assert analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league).endswith(INSIGHT)
    assert mock_requests.call_count == 1


def test_known_number_cannot_be_relabelled_as_a_different_team_metric(setup, mock_requests):
    league, context = setup
    context['league_history']['teams'][0]['performance'].update(
        average_margin=30, points_per_game_vs_league_average=50)
    bad = 'Oak is the scoring leader. They have outscored opponents by 50 points per game.'
    good = ('Oak has outscored opponents by 30 points per game; '
            'its scoring is doing the work, rather than a soft schedule.')
    mock_requests.post(ENDPOINT, [{'json': response(bad)}, {'json': response(good)}])
    result = analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league)
    assert result.endswith(good)
    assert mock_requests.call_count == 2


@pytest.mark.parametrize('claim', [
    'Oak is outside the mathematical bounds for the playoffs.',
    'Oak has been eliminated.',
    'Oak cannot make the playoffs.',
    'Oak is out of playoff contention.',
    'Oak has no mathematical chance of making the playoffs.',
    'Oak has clinched a playoff spot.',
])
def test_current_playoff_position_never_confirms_fate(setup, claim):
    _, context = setup
    report = ('Oak: #3 | Outside current playoff places; not confirmed eliminated\n'
              'Maple: #1 | Clinched by record bound\n'
              'These are sufficient mathematical bounds, not predicted odds.')
    assert 'unsupported_playoff_claim' in check_commentary(claim, report, context)


def test_confirmed_playoff_fate_is_bound_to_the_named_team(setup):
    _, context = setup
    report = 'Maple: #1 | Clinched by record bound\nOak: #4 | Eliminated by record bound'
    assert 'unsupported_playoff_claim' not in check_commentary('Maple has clinched.', report, context)
    assert 'unsupported_playoff_claim' not in check_commentary('Oak has been eliminated.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary('Oak has clinched.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary('Birch has been eliminated.', report, context)


@pytest.mark.parametrize('text', [
    'Oak is outside the current playoff places, not mathematically eliminated.',
    'No one has clinched anything yet.',
    'Oak has not yet clinched.',
    'Oak could be eliminated if the remaining results break against it.',
])
def test_uncertainty_is_not_a_false_playoff_announcement(setup, text):
    _, context = setup
    assert 'unsupported_playoff_claim' not in check_commentary(text, REPORT, context)


@pytest.mark.parametrize('name', ['Sports Team!!', 'Really? Yes!', 'St. Maple', 'O\u2019Maple'])
def test_confirmed_playoff_status_preserves_punctuation_in_team_names(name):
    context = {'players': [], 'fantasy_teams': [{'name': name}, {'name': 'Oak'}]}
    report = f'{name}: #1 | Clinched by record bound\nOak: #4 | Eliminated by record bound'
    assert 'unsupported_playoff_claim' not in check_commentary(f'{name} has clinched.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary(f'{name} has been eliminated.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary(f'{name} has clinched. Oak has clinched.', report, context)


def test_comma_aside_keeps_the_subject_of_confirmed_playoff_claim(setup):
    _, context = setup
    report = 'Maple: #1 | Clinched by record bound\nOak: #4 | Eliminated by record bound'
    assert 'unsupported_playoff_claim' not in check_commentary(
        'Maple, thanks to the record bound, has clinched.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary(
        'Oak, thanks to the record bound, has clinched.', report, context)


@pytest.mark.parametrize('separator', [', ', ', and ', '; ', ' while ', ' but '])
def test_coordinated_playoff_claims_require_each_teams_own_status(setup, separator):
    _, context = setup
    report = 'Maple: #1 | Clinched by record bound\nOak: #4 | Eliminated by record bound'
    assert 'unsupported_playoff_claim' not in check_commentary(
        f'Maple has clinched{separator}Oak has been eliminated.', report, context)
    assert 'unsupported_playoff_claim' in check_commentary(
        f'Maple has clinched{separator}Oak has clinched.', report, context)


def test_joint_playoff_claim_cannot_borrow_another_teams_confirmed_status(setup):
    _, context = setup
    report = 'Maple: #1 | Clinched by record bound\nOak: #4 | Eliminated by record bound'
    assert 'unsupported_playoff_claim' in check_commentary('Maple and Oak have clinched.', report, context)


@pytest.mark.parametrize('opening', ['Oak might recover', 'Oak could turn things around', 'Oak is still in contention'])
def test_independent_comma_clause_does_not_inherit_another_teams_uncertainty(setup, opening):
    _, context = setup
    claim = f'{opening}, Maple has clinched.'
    assert 'unsupported_playoff_claim' in check_commentary(claim, REPORT, context)
    assert 'unsupported_playoff_claim' not in check_commentary(
        claim, 'Maple: #1 | Clinched by record bound', context)
