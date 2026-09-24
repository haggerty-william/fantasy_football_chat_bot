import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from gamedaybot.espn import espn_read as reader


PUBLIC = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard'
FANTASY = reader.FANTASY_ROOT + '/2026/segments/0/leagues/123'


class Response:
    def __init__(self, chunks=None, status=200):
        self.status_code = status
        self.chunks = chunks if chunks is not None else [b'{"events": []}']
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        yield from self.chunks


@pytest.fixture
def http_call(monkeypatch):
    response = Response()
    http_call = Mock(return_value=response)
    monkeypatch.setattr(reader.requests, 'get', http_call)
    return http_call, response


@pytest.mark.parametrize('url', [
    'http://site.api.espn.com/apis/test', 'https://example.org/apis/test',
    'https://site.api.espn.com.evil.invalid/apis/test',
    'https://site.api.espn.com:444/apis/test', 'https://site.api.espn.com:secret/apis/test',
    'https://user:secret@site.api.espn.com/apis/test', 'https://site.api.espn.com/apis/test?secret=bad',
    'https://site.api.espn.com/apis/test#secret', 'https://site.api.espn.com/apis/../secret',
    'https://site.api.espn.com/apis/%2e%2e/secret', 'https://site.api.espn.com/apis//test',
    'https://site.api.espn.com\\evil.invalid/apis/test', 'https://site.api.espn.com/other/path',
    'https://[broken/apis/test', 'https://site.api.espn.com/apis/\nsecret', None,
    reader.FANTASY_ROOT + '/../../secret', reader.FANTASY_ROOT + '/2026/evil',
    reader.FANTASY_ROOT + '/2026/segments/0/leagues/123/transactions',
])
def test_bad_endpoint_never_reads_or_echoes_input(http_call, url):
    with pytest.raises(reader.ESPNReadError) as error:
        reader.read_espn(url)
    assert 'secret' not in str(error.value) and 'https://' not in str(error.value)
    http_call[0].assert_not_called()


def test_public_read_is_bounded_no_redirects_and_closes_response(http_call):
    result = reader.read_espn(PUBLIC, params={'week': 3}, deadline=time.monotonic() + 30)
    assert result == {'events': []}
    call = http_call[0].call_args
    assert call.args == (PUBLIC,)
    assert call.kwargs['stream'] is True and call.kwargs['allow_redirects'] is False
    assert all(0 < limit <= 5 for limit in call.kwargs['timeout'])
    assert call.kwargs['cookies'] is None and http_call[1].closed


def test_fantasy_credentials_only_go_to_fixed_fantasy_host(http_call):
    cookies = {'SWID': 'test-only', 'espn_s2': 'test-only'}
    with pytest.raises(reader.ESPNReadError):
        reader.read_espn(PUBLIC, cookies=cookies)
    http_call[0].assert_not_called()
    reader.read_espn(FANTASY, cookies=cookies)
    assert http_call[0].call_args.kwargs['cookies'] == cookies


def test_public_season_schedule_endpoint_is_supported(http_call):
    reader.read_espn(reader.FANTASY_ROOT + '/2026', params={'view': 'proTeamSchedules_wl'})
    assert http_call[0].call_args.args == (reader.FANTASY_ROOT + '/2026',)
    assert http_call[0].call_args.kwargs['cookies'] is None


@pytest.mark.parametrize('kwargs', [
    {'timeout': 0}, {'timeout': -1}, {'timeout': True}, {'timeout': float('inf')},
    {'timeout': float('nan')}, {'timeout': 31}, {'timeout': '8'},
    {'max_bytes': 0}, {'max_bytes': 10_000_001}, {'max_bytes': True},
    {'deadline': float('nan')}, {'deadline': float('inf')}, {'deadline': True},
])
def test_invalid_request_limits_do_not_read(http_call, kwargs):
    with pytest.raises(reader.ESPNReadError, match='limits'):
        reader.read_espn(PUBLIC, **kwargs)
    http_call[0].assert_not_called()


def test_expired_deadline_never_reads(http_call):
    with pytest.raises(reader.ESPNReadError, match='budget'):
        reader.read_espn(PUBLIC, deadline=time.monotonic() - 1)
    http_call[0].assert_not_called()


def test_response_cap_applies_across_chunks_and_closes(http_call):
    http_call[1].chunks = [b'{"abc":', b'"0123456789"}']
    with pytest.raises(reader.ESPNReadError, match='size limit'):
        reader.read_espn(PUBLIC, max_bytes=10)
    assert http_call[1].closed


def test_streaming_deadline_checked_between_chunks(http_call, monkeypatch):
    clock = iter([0, 0, 0, 2, 9])
    monkeypatch.setattr(reader.time, 'monotonic', lambda: next(clock))
    http_call[1].chunks = [b'{', b'}']
    with pytest.raises(reader.ESPNReadError, match='budget'):
        reader.read_espn(PUBLIC, timeout=8)
    assert http_call[1].closed


@pytest.mark.parametrize('status', [302, 401, 403, 404, 429, 500])
def test_http_failure_is_safe_and_never_follows_redirect(http_call, status):
    http_call[1].status_code = status
    http_call[1].chunks = [b'secret upstream response']
    with pytest.raises(reader.ESPNReadError) as error:
        reader.read_espn(PUBLIC)
    assert 'secret' not in str(error.value) and 'https://' not in str(error.value)
    assert http_call[1].closed


@pytest.mark.parametrize('chunks', [[b'not json: secret'], [b'123'], [b'null'], [b'"secret"'], [b'\xff']])
def test_invalid_json_or_shape_hides_body_and_closes(http_call, chunks):
    http_call[1].chunks = chunks
    with pytest.raises(reader.ESPNReadError) as error:
        reader.read_espn(PUBLIC)
    assert 'secret' not in str(error.value)
    assert http_call[1].closed


def test_network_error_hides_request_details(http_call):
    http_call[0].side_effect = requests.RequestException('secret-cookie https://private-url.invalid')
    with pytest.raises(reader.ESPNReadError) as error:
        reader.read_espn(FANTASY)
    assert str(error.value) == 'ESPN request or response unavailable.'


def test_read_league_builds_fixed_endpoint_and_filter_headers(http_call):
    league = SimpleNamespace(year=2026, league_id=123,
                             espn_request=SimpleNamespace(cookies={'SWID': 'test-only'}))
    reader.read_league(league, ['kona_playercard'], params={'scoringPeriodId': 3},
                       filters={'players': {'filterIds': {'value': [101]}}}, deadline=time.monotonic() + 30)
    call = http_call[0].call_args
    assert call.args == (FANTASY,)
    assert call.kwargs['params'] == {'scoringPeriodId': 3, 'view': ['kona_playercard']}
    assert json.loads(call.kwargs['headers']['x-fantasy-filter'])['players']['filterIds']['value'] == [101]
    assert call.kwargs['cookies'] == {'SWID': 'test-only'}


def test_communication_path_is_the_only_permitted_suffix(http_call):
    league = SimpleNamespace(year=2026, league_id=123)
    reader.read_league(league, 'kona_league_communication', path='/communication/')
    assert http_call[0].call_args.args == (FANTASY + '/communication/',)


@pytest.mark.parametrize('kwargs', [
    {'views': None}, {'views': [None]}, {'views': [{}]}, {'views': []},
    {'views': ['unverified_view']}, {'views': ['mRoster'], 'params': {'view': 'mSettings'}},
    {'views': ['mRoster'], 'params': 'bad'}, {'views': ['mRoster'], 'filters': {'bad': float('nan')}},
    {'views': ['mRoster'], 'path': '/write/players'},
])
def test_invalid_league_options_never_read(http_call, kwargs):
    with pytest.raises(reader.ESPNReadError):
        reader.read_league(SimpleNamespace(year=2026, league_id=123), **kwargs)
    http_call[0].assert_not_called()


@pytest.mark.parametrize('year,league_id', [(2026, '../private'), ('bad', 123), (True, 123), (2026, -1)])
def test_invalid_league_identity_never_reads(http_call, year, league_id):
    with pytest.raises(reader.ESPNReadError):
        reader.read_league(SimpleNamespace(year=year, league_id=league_id), ['mRoster'])
    http_call[0].assert_not_called()
