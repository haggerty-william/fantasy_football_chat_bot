import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from gamedaybot.espn.analysis import generate_analysis, _generation_lock, REPORT_CONTEXT

ENDPOINT = "http://localhost:1234/v1/chat/completions"
from gamedaybot.chat.discord_format import build_payloads


REPORT = 'Score Update\nOAK 100 - 90 MAP\n\nApproximate Projected Scores\nOAK 110 - 115 MAP'


@pytest.fixture
def api(monkeypatch, mock_requests):
    monkeypatch.setenv('AI_MODEL', 'qwen/qwen3.6-35b-a3b')
    monkeypatch.setenv('AI_BASE_URL', 'http://localhost:1234/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('AI_ANALYSIS', 'True')
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    return mock_requests


def complete(text='Oak leads, but Maple has the higher projection.'):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]}


def test_snapshot_and_context_are_sent_without_credentials(api):
    api.post(ENDPOINT, json=complete())
    result = generate_analysis(REPORT, 'get_scoreboard_short', week=3)
    body = api.last_request.json()
    snapshot = json.loads(body['messages'][1]['content'])
    assert snapshot['espn_report'] == REPORT
    assert snapshot['report_week'] == 3
    assert snapshot['snapshot_generated_at']
    assert 'Do not declare final winners' in body['messages'][0]['content']
    assert 'test-only-key' not in json.dumps(body)
    assert 'Authorization' not in api.last_request.headers
    assert body['reasoning_effort'] == 'none'
    assert body['stream'] is False
    assert body['max_tokens'] == 1400
    assert body['model'] == 'qwen/qwen3.6-35b-a3b'
    assert result.startswith('AI Analysis\nBased on ESPN report generated ')
    assert result.endswith('Oak leads, but Maple has the higher projection.')
    assert api.call_count == 1


@pytest.mark.parametrize('report_type', list(REPORT_CONTEXT))
@pytest.mark.parametrize('research_fails', [False, True])
def test_every_ai_report_keeps_manager_identity_even_when_research_fails(api, monkeypatch, report_type, research_fails):
    from gamedaybot.espn import analysis
    team = SimpleNamespace(team_id=1, team_name='Oak Club', owners=[{'firstName':'Tanner', 'lastName':'Example'}])
    league = SimpleNamespace(teams=[team])
    packet = {'players':[], 'news':[], 'rosters':[], 'fantasy_teams':[
        {'id':'1', 'name':'Oak Club', 'managers':['Tanner Example']} ]}
    monkeypatch.setattr(analysis, 'build_context', Mock(side_effect=RuntimeError('research failed')) if research_fails else Mock(return_value=packet))
    api.post(ENDPOINT, json=complete('A close result.'))
    assert generate_analysis('Oak Club report', report_type, league=league, week=2)
    raw = api.last_request.json()['messages'][1]['content']
    team_data = json.loads(raw)['research_context']['teams']['1']
    assert team_data['managers'] == ['Tanner Example']
    assert raw.count('Oak Club') == 1 and raw.count('Tanner Example') == 1
    reminder = json.loads(raw)['writing_reminder']
    assert 'When judging management decisions' in reminder


@pytest.mark.parametrize('status', [401, 403, 429, 500, 503])
def test_http_failures_skip_analysis(api, status, caplog):
    api.post(ENDPOINT, status_code=status, text='private-data test-only-key')
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
    assert 'test-only-key' not in caplog.text
    assert 'private-data' not in caplog.text
    assert api.call_count == 1


def test_timeout_keeps_failure_private(api, caplog):
    api.post(ENDPOINT, exc=requests.Timeout('test-only-key'))
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
    assert 'test-only-key' not in caplog.text


def test_longer_analysis_is_retained_and_formatted_within_discord_limits(api):
    from gamedaybot.chat.discord_format import build_payloads
    text = ('The matchup remains close. ' * 170) + 'Final observation.'
    api.post(ENDPOINT, json=complete(text))
    result = generate_analysis(REPORT, 'get_scoreboard_short')
    assert result.endswith(text)
    payloads = list(build_payloads(result))
    assert 1 <= len(payloads) <= 2
    embeds = [e for p in payloads for e in p['embeds']]
    assert all(len(e.get('description', '')) <= 4096 for e in embeds)
    assert 'Final observation.' in embeds[-1]['description']


@pytest.mark.parametrize('body', [
    {'choices': []}, {'choices': None},
    {'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': None}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'No', 'content': 'No'}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'tool_calls': [{}], 'content': 'No'}}]},
    complete(''), complete('x' * 6001), [],
    complete('<think>unfinished private reasoning'),
])
def test_unusable_response_skips_analysis(api, body):
    api.post(ENDPOINT, json=body)
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''


def test_invalid_json_skips_analysis(api):
    api.post(ENDPOINT, text='not json')
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''


def test_busy_model_does_not_queue_more_requests(api):
    _generation_lock.acquire()
    try:
        assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
        assert not api.called
    finally:
        _generation_lock.release()


def test_failure_releases_generation_slot(api):
    api.post(ENDPOINT, [{'exc': requests.Timeout()}, {'json': complete()}])
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
    assert generate_analysis(REPORT, 'get_scoreboard_short')


def test_reasoning_is_never_published(api):
    api.post(ENDPOINT, json=complete('<think>Private thoughts</think>Oak leads.'))
    result = generate_analysis(REPORT, 'get_scoreboard_short')
    assert result.endswith('Oak leads.')
    assert 'Private thoughts' not in result


def test_host_address_override_and_no_redirect_to_cloud(api, monkeypatch):
    monkeypatch.setenv('AI_BASE_URL', 'http://host.docker.internal:1234/v1/')
    endpoint = 'http://host.docker.internal:1234/v1/chat/completions'
    api.post(endpoint, status_code=302, headers={'Location': 'https://api.openai.com/v1/responses'})
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
    assert api.call_count == 1


@pytest.mark.parametrize('report,kind', [
    ('', 'get_trade_report'), ('No matchup data available.', 'get_final'),
    ('Welcome!', 'init'), ('Hello', 'broadcast'), ('x' * 12001, 'get_final'),
])
def test_ineligible_report_never_calls_local_model(api, report, kind):
    assert generate_analysis(report, kind) == ''
    assert not api.called


@pytest.mark.parametrize('setting', ['missing_model', 'disabled'])
def test_disabled_configuration_never_calls_local_model(api, monkeypatch, setting):
    if setting == 'missing_model':
        monkeypatch.delenv('AI_MODEL')
    else:
        monkeypatch.setenv('AI_ANALYSIS', 'False')
    assert generate_analysis(REPORT, 'get_scoreboard_short') == ''
    assert not api.called


def test_model_override_and_final_context(api, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'project-model')
    api.post(ENDPOINT, json=complete())
    generate_analysis('Final Score Update\nOAK 100 - 90 MAP', 'get_final', week=2)
    body = api.last_request.json()
    assert body['model'] == 'project-model'
    assert "final scores and awards for the week specified" in body['messages'][0]['content']
    assert json.loads(body['messages'][1]['content'])['report_week'] == 2


def test_commentary_has_its_own_discord_card(api):
    api.post(ENDPOINT, json=complete())
    text = REPORT + '\n\n' + generate_analysis(REPORT, 'get_scoreboard_short')
    embeds = [e for p in build_payloads(text) for e in p['embeds']]
    assert embeds[-1]['title'] == 'Graham Ellis'
    assert embeds[-1]['footer']['text'] == 'GameDayBot • AI commentary'
    assert embeds[0]['title'] == '🏈 Scoreboard'


@pytest.mark.parametrize('status', [200, 429])
def test_dispatch_delivers_report_even_when_ai_is_unavailable(api, monkeypatch, status):
    import gamedaybot.espn.espn_bot as bot

    api.post(ENDPOINT, status_code=status, json=complete())
    monkeypatch.setattr(bot, 'get_env_vars', lambda: {
        'str_limit': 1900, 'league_id': 123, 'discord_webhook_url': 'unused',
        'my_timezone': 'America/New_York',
    })
    league = SimpleNamespace(scoringPeriodId=3, firstScoringPeriod=1,
                             finalScoringPeriod=18, current_week=3)
    monkeypatch.setattr(bot, 'League', Mock(return_value=league))
    monkeypatch.setattr(bot, 'get_trade_report', Mock(return_value='Trade Report 2026-09-20:\nOak received Player 1'))
    destinations = []
    for name in ('Discord', 'Slack', 'GroupMe'):
        destination = Mock()
        monkeypatch.setattr(bot, name, Mock(return_value=destination))
        destinations.append(destination)
    bot.espn_bot('get_trade_report')
    for destination in destinations:
        destination.send_message.assert_called_once()
        sent = destination.send_message.call_args.args[0]
        assert sent.startswith('Trade Report 2026-09-20:\nOak received Player 1')
        assert ('AI Analysis' in sent) == (status == 200)
