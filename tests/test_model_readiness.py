from unittest.mock import Mock
import pytest
import requests
from gamedaybot.espn import model_readiness as m

URL='http://localhost:1234/api/v1/models'


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('AI_KEEP_MODEL_LOADED','True')
    monkeypatch.setenv('AI_ANALYSIS','True')
    monkeypatch.setenv('AI_MODEL','qwen/test')
    monkeypatch.setenv('AI_BASE_URL','http://localhost:1234/v1')


def models(loaded=False):
    return {'models':[{'key':'qwen/test','loaded_instances':[{'id':'qwen/test'}] if loaded else []},
                      {'key':'other/model','loaded_instances':[{'id':'other/model'}]}]}


def test_already_loaded_model_is_not_interrupted(configured,mock_requests):
    mock_requests.get(URL,json=models(True))
    assert m.ensure_model()
    assert mock_requests.call_count==1


def test_missing_instance_loaded_and_verified_without_ttl(configured,mock_requests):
    mock_requests.get(URL,[{'json':models()},{'json':models(True)}])
    mock_requests.post(URL+'/load',json={'status':'loaded','instance_id':'qwen/test'})
    assert m.ensure_model()
    body=mock_requests.request_history[1].json()
    assert body=={'model':'qwen/test','context_length':32768}
    assert len(mock_requests.request_history)==3


def test_load_response_alone_does_not_prove_readiness(configured,mock_requests):
    mock_requests.get(URL,json=models())
    mock_requests.post(URL+'/load',json={'status':'loaded'})
    assert not m.ensure_model()


def test_closed_server_fails_quickly_without_launching_apps(configured,mock_requests):
    mock_requests.get(URL,exc=requests.ConnectionError('private'))
    assert not m.ensure_model()
    assert mock_requests.call_count==1


def test_uninstalled_model_is_not_downloaded(configured,mock_requests):
    mock_requests.get(URL,json={'models':[]})
    assert not m.ensure_model()
    assert mock_requests.call_count==1


def test_disabled_keeper_does_not_contact_server(configured,mock_requests,monkeypatch):
    monkeypatch.setenv('AI_KEEP_MODEL_LOADED','False')
    assert m.ensure_model()
    assert not mock_requests.called


def test_keeper_only_starts_once(configured,monkeypatch):
    thread=Mock()
    thread.return_value.is_alive.return_value=True
    monkeypatch.setattr(m,'Thread',thread)
    monkeypatch.setattr(m,'_worker',None)
    m.start_model_keeper();m.start_model_keeper()
    thread.assert_called_once()
    thread.return_value.start.assert_called_once()
