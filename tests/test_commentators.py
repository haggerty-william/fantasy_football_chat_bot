"""Sequential voices share evidence/budgets and retain a verified first response."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from gamedaybot.commentator_names import ANALYST_NAME, RESPONDER_NAME
from gamedaybot.espn import analysis, commentators, research_tools
from gamedaybot.espn.commentary_checks import check_commentary as real_check_commentary


ENDPOINT = 'http://localhost:1234/v1/chat/completions'
ANALYST = 'Oak Club has the stronger all-play scoring; Tanner Example should treat the record as a small sample.'
COACH = 'The schedule owes somebody an apology, but a small sample is no license to declare a dynasty.'
REPORT = 'Current Standings\nOak Club: 1-1'


def complete(text):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]}


def tool_turn(prefix, count=3):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': f'{prefix}-{i}', 'type': 'function',
                        'function': {'name': 'get_league_personality', 'arguments': '{}'}} for i in range(count)]}}]}


@pytest.fixture
def booth(monkeypatch, mock_requests):
    monkeypatch.setenv('AI_SECOND_COMMENTATOR', 'True')
    monkeypatch.setenv('AI_ANALYSIS', 'True')
    monkeypatch.setenv('AI_MODEL', 'test-local-model')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    monkeypatch.setenv('AI_RESEARCH_TOOLS', 'True')
    team = SimpleNamespace(team_id=1, team_name='Oak Club', owners=[{'firstName': 'Tanner', 'lastName': 'Example'}])
    league = SimpleNamespace(teams=[team], year=2026, scoringPeriodId=3)
    context = {'week': 3, 'historical': False, 'players': [], 'news': [], 'rosters': [],
               'fantasy_teams': [{'id': '1', 'name': 'Oak Club', 'managers': ['Tanner Example']}],
               'limitations': []}
    monkeypatch.setattr(analysis, 'build_context', Mock(return_value=context))
    archive = Mock()
    monkeypatch.setattr(analysis, '_archive', archive)
    # Isolate orchestration from factual regex coverage in commentary_checks tests;
    # editorial duplication checks in commentators._issues remain real.
    from gamedaybot.espn import commentary_checks, commentary_quality
    monkeypatch.setattr(commentary_checks, 'check_commentary', lambda *args: [])
    monkeypatch.setattr(commentary_quality, 'check_analysis_value', lambda *args: [])
    return SimpleNamespace(api=mock_requests, league=league, context=context, archive=archive,
                           run=lambda: analysis.generate_analysis(REPORT, 'get_standings', week=3, league=league))


def test_second_voice_receives_validated_analyst_and_one_canonical_identity(booth):
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(COACH)}])
    result = booth.run()
    first, second = [request.json() for request in booth.api.request_history]
    assert f'You are {ANALYST_NAME}' in first['messages'][0]['content']
    assert f'You are {RESPONDER_NAME}' in second['messages'][0]['content']
    assert f'Your evidence-first colleague is {ANALYST_NAME}' in second['messages'][0]['content']
    assert first['model'] == second['model'] == 'test-local-model'
    packet = json.loads(second['messages'][1]['content'])
    assert packet['verified_analyst']['text'] == '[team:1] has the stronger all-play scoring; [manager:1:0] should treat the record as a small sample.'
    raw = second['messages'][1]['content']
    assert raw.count('Oak Club') == 1 and raw.count('Tanner Example') == 1
    assert result.index(ANALYST) < result.index('AI Hot Take') < result.index(COACH)
    assert ANALYST_NAME + ': ' + ANALYST in booth.archive.call_args.args[-1]
    assert RESPONDER_NAME + ': ' + COACH in booth.archive.call_args.args[-1]


def test_only_corrected_analyst_is_supplied_to_coach(booth, monkeypatch):
    from gamedaybot.espn import commentary_checks
    bad = 'Unverified original draft'
    monkeypatch.setattr(commentary_checks, 'check_commentary',
                        lambda text, *args: ['unsupported_claim'] if text == bad else [])
    booth.api.post(ENDPOINT, [{'json': complete(bad)}, {'json': complete(ANALYST)}, {'json': complete(COACH)}])
    result = booth.run()
    payload = booth.api.last_request.json()
    packet = json.loads(payload['messages'][1]['content'])
    assert '[team:1] has the stronger all-play' in packet['verified_analyst']['text']
    assert bad not in json.dumps(payload) and bad not in result
    assert ANALYST in result and COACH in result
    assert booth.api.call_count == 3


@pytest.mark.parametrize('failure', [
    {'status_code': 503, 'text': 'private-source-details'},
    {'exc': requests.Timeout('private-source-details')},
    {'text': 'not json private-source-details'},
    {'json': {'choices': []}},
    {'json': {'choices': None}},
    {'json': {'choices': [{'finish_reason': 'length', 'message': {'content': 'Incomplete coach'}}]}},
    {'json': {'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'No', 'content': 'No'}}]}},
    {'json': complete(None)}, {'json': complete('<think>unfinished')}, {'json': complete('x' * 6001)},
])
def test_second_stage_failures_keep_analyst_without_private_error_details(booth, failure, caplog):
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, failure])
    result = booth.run()
    assert ANALYST in result and 'AI Hot Take' not in result
    assert 'private-source-details' not in result and 'private-source-details' not in caplog.text
    assert booth.archive.call_args.args[-1] == ANALYST


def test_no_analyst_insight_skips_coach(booth):
    booth.api.post(ENDPOINT, json=complete('NO_ADDITIONAL_INSIGHT'))
    assert booth.run() == ''
    assert booth.api.call_count == 1
    booth.archive.assert_not_called()


def test_no_coach_insight_retains_analyst(booth):
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete('NO_ADDITIONAL_INSIGHT')}])
    result = booth.run()
    assert result.endswith(ANALYST) and 'NO_ADDITIONAL_INSIGHT' not in result


def test_unexpected_second_stage_setup_exception_retains_analyst(booth, monkeypatch, caplog):
    monkeypatch.setattr(commentators, 'hot_take', Mock(side_effect=ValueError('private setup details')))
    booth.api.post(ENDPOINT, json=complete(ANALYST))
    result = booth.run()
    assert result.endswith(ANALYST) and 'AI Hot Take' not in result
    assert 'private setup details' not in result and 'private setup details' not in caplog.text
    assert booth.archive.call_args.args[-1] == ANALYST


def test_repeated_coach_is_corrected_once_then_omitted(booth):
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(ANALYST)}, {'json': complete(ANALYST)}])
    result = booth.run()
    assert result.count(ANALYST) == 1 and 'AI Hot Take' not in result
    assert booth.api.call_count == 3
    assert 'second_voice_repeats_analyst' in booth.api.last_request.json()['messages'][-1]['content']
    assert booth.api.last_request.json()['tool_choice'] == 'none'


def test_distinct_coach_correction_can_be_used(booth):
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(ANALYST)}, {'json': complete(COACH)}])
    result = booth.run()
    assert ANALYST in result and COACH in result and 'AI Hot Take' in result


def test_invalid_coach_correction_preserves_analyst(booth, monkeypatch):
    from gamedaybot.espn import commentary_checks
    monkeypatch.setattr(commentary_checks, 'check_commentary',
                        lambda text, *args: [] if text == ANALYST else ['unsupported_claim'])
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(COACH)}, {'exc': requests.Timeout()}])
    result = booth.run()
    assert result.endswith(ANALYST) and 'AI Hot Take' not in result


def test_both_voices_share_the_same_four_round_twelve_call_budget(booth, monkeypatch):
    researchers, calls = [], []
    def execute(self, name, arguments):
        researchers.append(self)
        calls.append((name, arguments))
        self.calls += 1
        return {'notes': [], 'limitations': 'No user supplied lore.'}
    monkeypatch.setattr(research_tools.ResearchTools, 'execute', execute)
    booth.api.post(ENDPOINT, [
        {'json': tool_turn('analyst-1')}, {'json': tool_turn('analyst-2')}, {'json': complete(ANALYST)},
        {'json': tool_turn('coach-1')}, {'json': tool_turn('coach-2')}, {'json': complete(COACH)},
    ])
    assert COACH in booth.run()
    assert len(calls) == 12 and len({id(item) for item in researchers}) == 1
    assert researchers[0].calls == 12 and researchers[0].rounds == 4
    coach_start = booth.api.request_history[3].json()
    assert coach_start['tool_choice'] == 'auto'
    assert json.loads(coach_start['messages'][1]['content'])['verified_analyst']
    assert booth.api.last_request.json()['tool_choice'] == 'none'


def test_exhausted_analyst_rounds_leave_coach_synthesis_only(booth, monkeypatch):
    instances = []
    def execute(self, name, arguments):
        instances.append(self)
        self.calls += 1
        return {'notes': []}
    monkeypatch.setattr(research_tools.ResearchTools, 'execute', execute)
    responses = [{'json': tool_turn(f'analyst-{i}', 1)} for i in range(4)]
    responses.extend([{'json': complete(ANALYST)}, {'json': complete(COACH)}])
    booth.api.post(ENDPOINT, responses)
    assert COACH in booth.run()
    assert len(instances) == 4 and instances[0].rounds == 4
    assert booth.api.last_request.json()['tool_choice'] == 'none'


def test_coach_cannot_execute_tools_when_shared_rounds_are_exhausted(booth, monkeypatch):
    executions = []
    def execute(self, name, arguments):
        executions.append(name)
        self.calls += 1
        return {'notes': []}
    monkeypatch.setattr(research_tools.ResearchTools, 'execute', execute)
    responses = [{'json': tool_turn(f'analyst-{i}', 1)} for i in range(4)]
    responses.extend([{'json': complete(ANALYST)}, {'json': tool_turn('forbidden-coach')}])
    booth.api.post(ENDPOINT, responses)
    assert booth.run().endswith(ANALYST)
    assert len(executions) == 4


def test_second_voice_reuses_remaining_ten_minute_deadline(booth, monkeypatch):
    clock = [0.0]
    requests_seen = []
    monkeypatch.setattr(analysis.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(analysis, 'analysis_timeout', lambda: 600)
    def send(url, **kwargs):
        requests_seen.append(deepcopy(kwargs))
        is_coach = f'You are {RESPONDER_NAME}' in kwargs['json']['messages'][0]['content']
        clock[0] += 200 if is_coach else 360
        return SimpleNamespace(status_code=200, json=lambda: complete(COACH if is_coach else ANALYST))
    monkeypatch.setattr(analysis.requests, 'post', send)
    result = booth.run()
    assert COACH in result and len(requests_seen) == 2
    assert requests_seen[0]['timeout'][1] <= 375
    assert requests_seen[1]['timeout'][1] <= 235
    assert clock[0] == 560


def test_no_remaining_time_skips_second_generation_and_keeps_analyst(booth, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(analysis.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(analysis, 'analysis_timeout', lambda: 600)
    # Simulate deadline use by a completed first stage; no second request should start.
    def send(url, **kwargs):
        clock[0] = 590
        return SimpleNamespace(status_code=200, json=lambda: complete(ANALYST))
    sender = Mock(side_effect=send)
    monkeypatch.setattr(analysis.requests, 'post', sender)
    assert booth.run().endswith(ANALYST)
    sender.assert_called_once()


def test_feature_flag_retains_single_analyst_flow(booth, monkeypatch):
    monkeypatch.setenv('AI_SECOND_COMMENTATOR', 'False')
    booth.api.post(ENDPOINT, json=complete(ANALYST))
    assert booth.run().endswith(ANALYST)
    assert booth.api.call_count == 1
    assert f'You are {ANALYST_NAME}' not in booth.api.last_request.json()['messages'][0]['content']


METRIC_ANALYST = ('Oak Club scores 47.19 points above the league average, while Pine is 32.38 below. '
                  'The standings disguise that difference in production.')
METRIC_PARAPHRASE = ('The attack in Oak Club has been running 47.19 clear of the neighborhood norm. '
                     'Pine sits a miserable 32.38 below it. Somebody owes the scoring table an apology.')


def test_two_repeated_decimal_statistics_trigger_repair_despite_different_prose(booth):
    booth.api.post(ENDPOINT, [{'json': complete(METRIC_ANALYST)},
                              {'json': complete(METRIC_PARAPHRASE)}, {'json': complete(COACH)}])
    result = booth.run()
    assert booth.api.call_count == 3
    correction = booth.api.last_request.json()['messages'][-1]['content']
    assert 'second_voice_repeats_analyst' in correction
    assert METRIC_PARAPHRASE not in result and METRIC_ANALYST in result and COACH in result


@pytest.mark.parametrize('reaction', [
    'A 47.19-point advantage over the league average buys some swagger; it does not buy immunity from a terrible week.',
    'A 47.19-point gap is swagger material. Treating 47.19 as an eternal entitlement would be the real comedy.',
])
def test_one_distinct_repeated_supporting_figure_is_allowed(booth, reaction):
    booth.api.post(ENDPOINT, [{'json': complete(METRIC_ANALYST)}, {'json': complete(reaction)}])
    result = booth.run()
    assert booth.api.call_count == 2
    assert result.endswith(reaction) and 'AI Hot Take' in result


@pytest.mark.parametrize('repair_succeeds', [False, True])
def test_overlong_second_voice_is_corrected_once_or_omitted(booth, repair_succeeds):
    verbose = ('A lucky result deserves a raised eyebrow, since the underlying scoring evidence still needs scrutiny. ' * 9).strip()
    assert len(verbose.split()) > 130
    repaired = COACH if repair_succeeds else verbose
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(verbose)}, {'json': complete(repaired)}])
    result = booth.run()
    assert booth.api.call_count == 3
    assert ANALYST in result and verbose not in result
    assert ('AI Hot Take' in result) is repair_succeeds
    assert result.endswith(COACH if repair_succeeds else ANALYST)


def test_second_packet_assignment_replaces_first_role_and_survives_tool_refresh(booth, monkeypatch):
    def execute(self, name, arguments):
        self.calls += 1
        return {'notes': [], 'limitations': 'No supplied lore.'}
    monkeypatch.setattr(research_tools.ResearchTools, 'execute', execute)
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': tool_turn('coach-research', 1)},
                              {'json': complete(COACH)}])
    assert COACH in booth.run()
    packets = [json.loads(request.json()['messages'][1]['content']) for request in booth.api.request_history]
    first_role = packets[0]['commentary_assignment']
    assert first_role.lstrip().startswith(f'You are {ANALYST_NAME}')
    for packet in packets[1:]:
        assignment = packet['commentary_assignment']
        assert assignment.lstrip().startswith(f'You are {RESPONDER_NAME}')
        assert first_role not in assignment
        assert 'React to' in assignment
        assert packet['verified_analyst']['speaker'] == ANALYST_NAME
        assert '[team:1] has the stronger all-play scoring' in packet['verified_analyst']['text']


def test_known_manager_reference_is_resolved_in_published_hot_take(booth):
    reaction = '[manager:1:0] should keep the champagne corked; small samples are not a championship reservation.'
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(reaction)}])
    result = booth.run()
    assert 'AI Hot Take\nTanner Example should keep the champagne corked' in result
    assert '[manager:' not in result and '[team:' not in result
    assert '[manager:' not in booth.archive.call_args.args[-1]
    assert booth.api.call_count == 2


@pytest.mark.parametrize('repair_succeeds', [False, True])
def test_unknown_manager_reference_is_repaired_or_omitted_without_losing_analyst(booth, monkeypatch, repair_succeeds):
    from gamedaybot.espn import commentary_checks
    monkeypatch.setattr(commentary_checks, 'check_commentary', real_check_commentary)
    invalid = '[manager:999:0] has booked a championship parade on a weather forecast.'
    valid = '[manager:1:0] should keep the champagne corked; small samples are not a championship reservation.'
    booth.api.post(ENDPOINT, [{'json': complete(ANALYST)}, {'json': complete(invalid)},
                              {'json': complete(valid if repair_succeeds else invalid)}])
    result = booth.run()
    assert ANALYST in result
    assert '[manager:' not in result and '[team:' not in result
    assert 'unresolved_identity_reference' in booth.api.last_request.json()['messages'][-1]['content']
    assert booth.api.call_count == 3
    assert ('AI Hot Take' in result) is repair_succeeds
    if repair_succeeds:
        assert result.endswith(valid.replace('[manager:1:0]', 'Tanner Example'))
    else:
        assert result.endswith(ANALYST)
