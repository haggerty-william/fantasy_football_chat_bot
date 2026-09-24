import pytest
import requests_mock


@pytest.fixture(autouse=True)
def isolate_model_readiness(monkeypatch):
    # Inference tests mock chat completions; model-management tests explicitly
    # enable and exercise the separate load/check API.
    monkeypatch.setenv('AI_KEEP_MODEL_LOADED', 'False')
    # Legacy tests exercise the analyst independently. Dual-commentator tests
    # explicitly enable and verify the full sequential flow.
    monkeypatch.setenv('AI_SECOND_COMMENTATOR', 'False')


@pytest.fixture
def mock_requests():
    with requests_mock.Mocker() as m:
        yield m
