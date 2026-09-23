"""Keep the configured local model available while its API server is running."""
import logging
import os
from threading import Event, Lock, Thread
import time

import requests

from gamedaybot.utils.util import str_to_bool

logger = logging.getLogger(__name__)
_load_lock = Lock()
_start_lock = Lock()
_worker = None
_stop = Event()


def enabled():
    return (str_to_bool(os.environ.get('AI_ANALYSIS', 'True'))
            and str_to_bool(os.environ.get('AI_KEEP_MODEL_LOADED', 'True'))
            and bool(os.environ.get('AI_MODEL', '').strip()))


def ensure_model(timeout=90):
    """Check before generation; serialize loading with the background keeper.

    Explicit API loading creates a manual instance without an idle TTL. Never
    unload a running instance or a different model; no app launch or downloads.
    """
    if not enabled(): return True
    deadline = time.monotonic() + timeout
    if not _load_lock.acquire(timeout=max(0, timeout)):
        return False
    try:
        model = os.environ['AI_MODEL'].strip()
        base = os.environ.get('AI_BASE_URL', 'http://localhost:1234/v1').strip().rstrip('/')
        root = base[:-3] if base.endswith('/v1') else base
        endpoint = root + '/api/v1/models'

        def models():
            remaining = deadline - time.monotonic()
            if remaining < 1: raise requests.Timeout()
            r = requests.get(endpoint, timeout=(3, min(8, remaining)), allow_redirects=False)
            r.raise_for_status()
            return r.json()['models']

        rows = models()
        target = next((r for r in rows if r.get('key') == model or
                       any(i.get('id') == model for i in r.get('loaded_instances', []))), None)
        if target is None:
            logger.warning('Configured local model is not installed: %s', model)
            return False
        if target.get('loaded_instances'):
            return True
        remaining = deadline - time.monotonic() - 10
        if remaining < 1: return False
        try: context_length = max(8192, min(65536, int(os.environ.get('AI_MODEL_CONTEXT_LENGTH', '32768'))))
        except ValueError: context_length = 32768
        logger.warning('Loading configured local model: %s', model)
        r = requests.post(endpoint + '/load', json={'model': target['key'], 'context_length': context_length},
                          timeout=(3, remaining), allow_redirects=False)
        r.raise_for_status()
        # Verify actual server state; do not trust a queued/partial load response.
        ready = any(row.get('key') == target['key'] and row.get('loaded_instances') for row in models())
        if ready: logger.warning('Configured local model ready: %s', model)
        return ready
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        logger.info('Local model server unavailable; readiness will be checked again')
        return False
    finally:
        _load_lock.release()


def start_model_keeper():
    global _worker
    if not enabled(): return
    with _start_lock:
        if _worker is not None and _worker.is_alive(): return
        _stop.clear()

        def run():
            while not _stop.is_set():
                try: ensure_model()
                except Exception:
                    logger.warning('Model readiness check failed; retrying later')
                _stop.wait(60)

        _worker = Thread(target=run, name='local-model-readiness', daemon=True)
        _worker.start()
