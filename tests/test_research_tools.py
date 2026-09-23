import json
import time
from unittest.mock import Mock

import pytest

from gamedaybot.espn import analysis, research_tools

ENDPOINT = 'http://localhost:1234/v1/chat/completions'


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'local')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    context = {'week': 2, 'historical': False, 'players': [], 'news': [],
               'rosters': [{'team': 'Oak', 'players': [[1, 'Player Alpha', 'WR', 'WR', 10, 12, 'ACTIVE', 'BUF']]}]}
    packet = {'source': 'ESPN Fantasy', 'fetched_at': 'now', 'week': 2, 'limitations': [],
              'players': [{'id': 1, 'name': 'Player Alpha', 'points': 17, 'current_status': 'QUESTIONABLE',
                           'stats': {'receivingTargets': 8}, 'usage': {'weeks': [{'week': 1, 'targets': 8}]}}],
              'news': [{'headline': 'Player Alpha practiced', 'published_at': 'today'}]}
    fetch = Mock(return_value=packet)
    monkeypatch.setattr(research_tools, 'build_context', fetch)
    monkeypatch.setattr(analysis, 'build_context', lambda *a, **k: context)
    return context, fetch


def tool_response(name='get_player_stats', args='{"player_ids":["1"]}'):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
        'tool_calls': [{'id': 'research1', 'type': 'function', 'function': {'name': name, 'arguments': args}}]}}]}


def completed(text):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}


def test_tool_evidence_reaches_model_and_validator(setup, mock_requests):
    mock_requests.post(ENDPOINT, [{'json': tool_response()}, {'json': completed('Player Alpha scored 17 points.')}])
    result = analysis.generate_analysis('Scores', 'get_scoreboard_short', league=object(), week=2)
    assert result.startswith('AI Analysis') and '17 points' in result
    body = mock_requests.last_request.json()
    tool = next(m for m in body['messages'] if m['role'] == 'tool')
    assert tool['tool_call_id'] == 'research1'
    receipt = json.loads(tool['content'])
    evidence = json.loads(body['messages'][1]['content'])
    for key in receipt['evidence_paths'][0]:
        evidence = evidence[key]
    assert evidence['data']['players'][0]['points'] == 17
    assert setup[0]['players'][0]['points'] == 17
    assert mock_requests.call_count == 2


@pytest.mark.parametrize('name,args', [
    ('fetch_url', '{"url":"http://localhost/secrets"}'),
    ('get_player_stats', '{"player_ids":["999"]}'),
    ('get_player_stats', '{"player_ids":["1"],"url":"http://localhost"}'),
    ('get_player_stats', '{"player_ids":[1]}'),
    ('get_player_stats', 'not json'),
    ('get_player_stats', '[]'),
])
def test_untrusted_arguments_never_fetch(setup, name, args):
    context, fetch = setup
    tools = research_tools.ResearchTools(object(), context, 2, None, time.monotonic()+60)
    assert 'error' in tools.execute(name, args)
    fetch.assert_not_called()


def test_historical_and_expired_research_do_not_fetch(setup):
    context, fetch = setup
    context['historical'] = True
    tools = research_tools.ResearchTools(object(), context, 1, None, time.monotonic()+60)
    for name in ('get_player_news', 'get_player_status'):
        assert 'error' in tools.execute(name, '{"player_ids":["1"]}')
    tools.deadline = time.monotonic()
    assert 'error' in tools.execute('get_player_stats', '{"player_ids":["1"]}')
    fetch.assert_not_called()


def test_player_fetch_reused_for_different_tools(setup):
    context, fetch = setup
    tools = research_tools.ResearchTools(object(), context, 2, None, time.monotonic()+60)
    news = tools.execute('get_player_news', '{"player_ids":["1"]}')
    stats = tools.execute('get_player_stats', '{"player_ids":["1"]}')
    assert news['news'] and 'points' not in news['players'][0]
    assert stats['players'][0]['points'] == 17
    fetch.assert_called_once()


def test_tools_stop_after_four_rounds(setup, mock_requests):
    mock_requests.post(ENDPOINT, [{'json': tool_response()}, {'json': tool_response('get_player_status')},
                                 {'json': tool_response()}, {'json': tool_response('get_player_status')},
                                 {'json': completed('Player Alpha could help.')}])
    assert analysis.generate_analysis('Scores', 'get_scoreboard_short', league=object(), week=2).startswith('AI Analysis')
    assert mock_requests.last_request.json()['tool_choice'] == 'none'
    assert mock_requests.call_count == 5


def test_three_tool_calls_per_round_and_twelve_total(setup, mock_requests, monkeypatch):
    executed = []
    def execute(self, name, args):
        self.calls += 1
        executed.append((name, args))
        return {'notes': []}
    monkeypatch.setattr(research_tools.ResearchTools, 'execute', execute)
    responses = []
    for round_number in range(4):
        response = tool_response('get_league_personality', '{}')
        call = response['choices'][0]['message']['tool_calls'][0]
        response['choices'][0]['message']['tool_calls'] = [dict(call, id=f'call-{round_number}-{i}') for i in range(3)]
        responses.append({'json': response})
    responses.append({'json': completed('No league lore has been supplied.')})
    mock_requests.post(ENDPOINT, responses)
    assert analysis.generate_analysis('Scores', 'get_scoreboard_short', league=object(), week=2)
    assert len(executed) == 12
    assert mock_requests.last_request.json()['tool_choice'] == 'none'


def test_source_failure_still_allows_commentary(setup, mock_requests):
    setup[1].side_effect = RuntimeError('private details')
    mock_requests.post(ENDPOINT, [{'json': tool_response()}, {'json': completed('Additional player details are unavailable.')}])
    assert analysis.generate_analysis('Scores', 'get_scoreboard_short', league=object(), week=2).startswith('AI Analysis')
    body = json.dumps(mock_requests.last_request.json())
    assert 'Source unavailable' in body and 'private details' not in body


def test_tools_can_be_disabled(setup, mock_requests, monkeypatch):
    monkeypatch.setenv('AI_RESEARCH_TOOLS', 'False')
    mock_requests.post(ENDPOINT, json=completed('Additional details are unavailable.'))
    assert analysis.generate_analysis('Scores', 'get_scoreboard_short', league=object(), week=2)
    assert 'tools' not in mock_requests.last_request.json()
