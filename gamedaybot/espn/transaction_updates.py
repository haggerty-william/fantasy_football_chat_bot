"""Read-only hourly transaction change detection, independent of delivery.

The caller must persist returned state and queued events atomically. A first
successful read establishes a quiet baseline; failures never advance it. ESPN's
pending feed is account-limited, so disappearance never means cancellation or
completion. Only the existing completed-trade notifier verifies transfers.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
from types import SimpleNamespace

from gamedaybot.espn import transaction_tools


_PROPOSAL_LIMIT = 4096
_ROSTER_LIMIT = 8192
_FINISHED = frozenset(('EXECUTED', 'COMPLETE', 'COMPLETED'))
_VISIBILITY = transaction_tools._SCOPE


class TransactionUpdateUnavailable(RuntimeError):
    """A source failed or omitted its collection; keep the previous state."""


def _stamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _observed(value):
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    stamp = _stamp(value)
    if stamp is None:
        raise ValueError('observed_at must be an ISO date or datetime')
    return stamp


def _researcher(league):
    week = getattr(league, 'scoringPeriodId', None)
    if type(week) is not int or not 1 <= week <= 25:
        raise TransactionUpdateUnavailable('ESPN scoring week is unavailable.')
    return SimpleNamespace(league=league, week=week, context={},
                           deadline=time.monotonic() + 20)


def _state(league, previous, observed, source):
    scope = '{}:{}'.format(getattr(league, 'league_id', ''), getattr(league, 'year', ''))
    baseline = not isinstance(previous, dict) or previous.get('scope') != scope or previous.get('source') != source
    if baseline:
        return {'version': 1, 'source': source, 'scope': scope,
                'initialized_at': observed, 'observed_at': observed, 'seen': {},
                'visibility': _VISIBILITY, 'truncated': False}, True
    result = deepcopy(previous)
    result['observed_at'] = observed
    if not isinstance(result.get('seen'), dict):
        raise ValueError('Transaction update state has an invalid seen ledger')
    return result, False


def _event_id(scope, kind, identifier, signature=None):
    fields = [scope, kind, identifier]
    if signature is not None:
        fields.append(signature)
    return kind + ':' + hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()[:32]


def _retired(state, when):
    """A compacted ledger never replays an old or undated forgotten record."""
    if not state.get('ledger_compacted'):
        return False
    return when is None or when <= state.get('replay_before', '')


def _bound(state, limit):
    seen = state['seen']
    if len(seen) <= limit:
        return
    # Active proposals keep their place. Once old identities are evicted, the
    # watermark prevents those old records announcing again on reappearance.
    retired = sorted(seen, key=lambda key: (seen[key].get('last_seen_at', ''), key))[:len(seen) - limit]
    cutoff = state.get('replay_before', '')
    for key in retired:
        cutoff = max(cutoff, seen[key].get('event_at') or seen[key].get('first_seen_at', ''))
        del seen[key]
    state['ledger_compacted'] = True
    state['replay_before'] = cutoff


def _proposal_signature(row):
    status, state = row.get('source_status'), row.get('state')
    if state == 'closed_without_verified_completion' and status in transaction_tools._CLOSED:
        return 'closed:' + ('CANCELED' if status == 'CANCELLED' else status)
    if state == 'accepted_awaiting_processing':
        # AcceptedDate and TRADE_ACCEPT both establish acceptance, never the
        # actual exchange of players, including an EXECUTED acceptance event.
        return 'accepted_awaiting_processing'
    if state in ('proposed', 'pending_state_unconfirmed') and status in ('PENDING', 'PROPOSED'):
        return 'proposed'
    return None


def collect_proposals(league, previous=None, observed_at=None):
    """Return newly visible offers and explicit lifecycle changes, once each.

    Existing offers form the initial baseline. Each event ID includes its
    lifecycle signature, while ``trade.id`` remains the provider's stable offer
    identity. Closed offers remain explicitly unverified as completed. Partial
    feeds retain all prior identities and never infer changes from omissions.
    """
    observed = _observed(observed_at)
    state, baseline = _state(league, previous, observed, 'trade_proposals')
    try:
        result = transaction_tools._pending(_researcher(league), {'limit': 100}, timeout=8)
    except Exception:
        raise TransactionUpdateUnavailable('ESPN pending trades are unavailable; previous state must be retained.') from None
    if result.get('error') or result.get('status') == 'not_exposed' or not isinstance(result.get('trades'), list):
        raise TransactionUpdateUnavailable('ESPN did not expose pending trades; previous state must be retained.')
    events = []
    state['truncated'] = bool(result.get('truncated'))
    state['source_status'] = result.get('status')
    state['visible_count'] = result.get('visible_count')
    state['unconfirmed_count'] = 0
    for supplied in result['trades']:
        row = deepcopy(supplied)
        identifier = row.get('id')
        if not isinstance(identifier, str) or not identifier:
            continue
        row['completed'] = False
        signature = _proposal_signature(row)
        old = state['seen'].get(identifier)
        record = old or {'signatures': [], 'first_seen_at': observed}
        when = _stamp(row.get('proposed_at'))
        record.update(last_seen_at=observed, event_at=when)
        state['seen'][identifier] = record
        if signature is None:
            state['unconfirmed_count'] += 1
            continue
        if signature in record['signatures']:
            continue
        # There are a bounded number of explicit ESPN states. Keeping every
        # observed state prevents repeated messages if a stale feed oscillates.
        record['signatures'].append(signature)
        if baseline or (old is None and _retired(state, when)):
            continue
        events.append({'id': _event_id(state['scope'], 'trade_proposal', identifier, signature),
                       'kind': 'trade_proposal', 'observed_at': observed,
                       'change': 'status_changed' if old else 'newly_visible',
                       'visibility': _VISIBILITY, 'trade': row})
    _bound(state, _PROPOSAL_LIMIT)
    events.sort(key=lambda event: (event['trade'].get('accepted_at') or event['trade'].get('proposed_at') or '', event['id']))
    return events, state


def collect_roster_moves(league, previous=None, observed_at=None):
    """Return completed public player adds/drops from this and the prior week.

    Lineup moves, pending claims, failed claims and bid amounts never enter the
    event stream. The baseline date also suppresses previously hidden backlog
    after a truncated first fetch; undated records cannot establish a new move.
    """
    observed = _observed(observed_at)
    state, baseline = _state(league, previous, observed, 'roster_moves')
    researcher = _researcher(league)
    weeks = list(range(max(1, researcher.week - 1), researcher.week + 1))
    rows, truncated = [], False
    try:
        for week in weeks:
            result = transaction_tools._history(researcher, {'week': week, 'limit': 200, 'types': ['FREEAGENT', 'WAIVER']})
            if result.get('error') or not isinstance(result.get('transactions'), list):
                raise TransactionUpdateUnavailable('ESPN did not expose completed roster moves.')
            rows.extend(result['transactions'])
            truncated = truncated or bool(result.get('truncated'))
    except Exception:
        raise TransactionUpdateUnavailable('ESPN completed roster moves are unavailable; previous state must be retained.') from None
    events = []
    state.update(scanned_weeks=weeks, truncated=truncated, undated_count=0)
    for supplied in rows:
        if supplied.get('type') not in ('FREEAGENT', 'WAIVER') or supplied.get('source_status') not in _FINISHED:
            continue
        row = deepcopy(supplied)
        items = [item for item in row.get('items', []) if item.get('type') in ('ADD', 'DROP')]
        if not items:
            continue
        # Do not convert lineup changes or other transaction legs into moves.
        if len(items) != len(row['items']):
            row['items_complete'] = False
        row['items'] = items
        row['completed'] = True
        identifier = row.get('id')
        if not isinstance(identifier, str) or not identifier:
            continue
        when = _stamp(row.get('processed_at'))
        old = state['seen'].get(identifier)
        state['seen'][identifier] = {'first_seen_at': old['first_seen_at'] if old else observed,
                                     'last_seen_at': observed, 'event_at': when}
        if when is None:
            state['undated_count'] += 1
        if baseline or old or when is None or when <= state['initialized_at'] or _retired(state, when):
            continue
        events.append({'id': _event_id(state['scope'], 'roster_move', identifier),
                       'kind': 'roster_move', 'observed_at': observed, 'transaction': row})
    _bound(state, _ROSTER_LIMIT)
    events.sort(key=lambda event: (event['transaction']['processed_at'], event['id']))
    return events, state
