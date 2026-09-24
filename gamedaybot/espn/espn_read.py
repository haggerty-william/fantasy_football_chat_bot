"""Bounded reads from fixed ESPN services; never a model-directed HTTP client."""
import json
import math
import re
import time
from urllib.parse import urlsplit

import requests


FANTASY_ROOT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons'
PUBLIC_HOSTS = {'site.api.espn.com', 'site.web.api.espn.com', 'sports.core.api.espn.com'}
VIEWS = {'mSettings', 'mTeam', 'mRoster', 'mMatchup', 'mMatchupScore', 'mScoreboard',
         'mStandings', 'mStatus', 'mDraftDetail', 'mDraft', 'mSchedule', 'mBoxscore',
         'mPendingTransactions', 'mTransactions2', 'kona_league_communication',
         'kona_player_info', 'kona_playercard', 'mLiveScoring', 'mNav'}


class ESPNReadError(ValueError):
    """Safe error category; never includes a URL, cookie or response body."""


def read_espn(url, *, params=None, headers=None, cookies=None, deadline=None,
              timeout=8, max_bytes=2_000_000):
    try:
        if (not isinstance(url, str) or len(url) > 512 or re.search(r'[\s\\%]', url)):
            raise ValueError()
        parsed = urlsplit(url)
        fantasy = parsed.hostname == 'lm-api-reads.fantasy.espn.com'
        fantasy_path = bool(re.fullmatch(
            r'/apis/v3/games/ffl/seasons/[0-9]{4}(?:/segments/0/leagues/[0-9]{1,20}(?:/communication/)?)?', parsed.path))
        valid = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                 and '..' not in parsed.path and '//' not in parsed.path
                 and (fantasy and fantasy_path or parsed.hostname in PUBLIC_HOSTS and parsed.path.startswith('/apis/')))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ESPNReadError('Unsupported ESPN endpoint.')
    if cookies and not fantasy:
        raise ESPNReadError('Fantasy credentials cannot be sent to public endpoints.')
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout)
            or not 0 < timeout <= 30 or type(max_bytes) is not int or not 1 <= max_bytes <= 10_000_000
            or (deadline is not None and (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                                         or not math.isfinite(deadline)))):
        raise ESPNReadError('Invalid ESPN request limits.')
    until = min(time.monotonic() + timeout, deadline) if deadline is not None else time.monotonic() + timeout
    remaining = until - time.monotonic()
    if remaining < .25:
        raise ESPNReadError('Research time budget exhausted.')
    try:
        with requests.get(url, params=params, headers=headers, cookies=cookies,
                          timeout=(min(3, remaining / 2), min(5, remaining / 2)),
                          allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ESPNReadError('ESPN data unavailable (HTTP %s).' % response.status_code)
            if time.monotonic() >= until:
                raise ESPNReadError('Research time budget exhausted.')
            chunks, length = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() >= until:
                    raise ESPNReadError('Research time budget exhausted.')
                length += len(chunk)
                if length > max_bytes:
                    raise ESPNReadError('ESPN response exceeds the research size limit.')
                chunks.append(chunk)
            result = json.loads(b''.join(chunks))
            if time.monotonic() >= until:
                raise ESPNReadError('Research time budget exhausted.')
            if not isinstance(result, (dict, list)):
                raise ESPNReadError('Unexpected ESPN response shape.')
            return result
    except ESPNReadError:
        raise
    except (requests.RequestException, ValueError, TypeError, UnicodeError):
        raise ESPNReadError('ESPN request or response unavailable.') from None


def read_league(league, views, *, params=None, filters=None, deadline=None,
                timeout=8, max_bytes=2_000_000, path=''):
    if path not in ('', '/communication/'):
        raise ESPNReadError('Unsupported fantasy resource.')
    if not isinstance(views, (str, list, tuple)):
        raise ESPNReadError('Unsupported fantasy view.')
    views = [views] if isinstance(views, str) else list(views)
    if not views or any(not isinstance(view, str) or view not in VIEWS for view in views):
        raise ESPNReadError('Unsupported fantasy view.')
    year, league_id = str(getattr(league, 'year', '')), str(getattr(league, 'league_id', ''))
    if not year.isdigit() or not league_id.isdigit() or len(year) != 4 or len(league_id) > 20:
        raise ESPNReadError('Verified league identity unavailable.')
    if params is not None and not isinstance(params, dict):
        raise ESPNReadError('Unsupported fantasy query.')
    query = dict(params or {})
    if 'view' in query:
        raise ESPNReadError('Specify fantasy views separately.')
    query['view'] = views
    try:
        headers = {'x-fantasy-filter': json.dumps(filters, separators=(',', ':'), allow_nan=False)} if filters is not None else None
    except (ValueError, TypeError):
        raise ESPNReadError('Unsupported fantasy filters.') from None
    cookies = getattr(getattr(league, 'espn_request', None), 'cookies', None)
    return read_espn(f'{FANTASY_ROOT}/{year}/segments/0/leagues/{league_id}{path}',
                     params=query, headers=headers, cookies=cookies, deadline=deadline,
                     timeout=timeout, max_bytes=max_bytes)
