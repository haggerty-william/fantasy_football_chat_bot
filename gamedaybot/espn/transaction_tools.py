"""Bounded, read-only ESPN trade and transaction evidence.

Accepted proposals and completed player transfers are separate events. Only the
communication feed's TRADED messages establish completed trades here; neither
an accepted date nor an EXECUTED acceptance event proves a roster transfer.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
from types import SimpleNamespace

from gamedaybot.espn.decision_tools import depth, lineup, tool


TEAM = {'team_id': {'type': 'string', 'description': 'Optional exact fantasy team ID from the supplied teams directory.'}}
LIMIT = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'description': 'Maximum returned records; default 10.'}}
HISTORY_TYPES = frozenset(('FREEAGENT', 'WAIVER', 'TRADE_ACCEPT', 'TRADE_PROPOSAL',
                          'TRADE_VETO', 'TRADE_UPHOLD', 'TRADE_DECLINE',
                          'ROSTER', 'FUTURE_ROSTER', 'RETRO_ROSTER'))
TOOLS = [
    tool('get_recent_trades', 'Read completed player transfers from ESPN trade activity, including exact sending and receiving teams. Separate from pending offers; capped recent history.',
         {**TEAM, **LIMIT, 'days': {'type': 'integer', 'minimum': 1, 'maximum': 60}}, []),
    tool('get_pending_trades', 'Read visible pending trade proposals and accepted trades awaiting processing. Account-limited visibility; absence does not prove nobody has an offer. Never treats pending trades as completed.',
         {**TEAM, **LIMIT}, []),
    tool('get_transaction_history', 'Read a scoring week of player adds, drops, waiver acquisitions, roster moves and trade lifecycle events. Trade acceptance is not completion; no private bid amounts or waiver claims.',
         {**TEAM, **LIMIT, 'week': {'type': 'integer', 'minimum': 1, 'maximum': 18},
          'types': {'type': 'array', 'minItems': 1, 'maxItems': 6,
                    'items': {'type': 'string', 'enum': sorted(HISTORY_TYPES)}}}, []),
    tool('evaluate_trade_proposal', 'Estimate a hypothetical or pending exchange using current owned players, roster depth and verified ESPN weekly projections. Does not propose, accept or execute a trade; no invented rest-of-season value.',
         {'team_a_id': {'type': 'string'}, 'team_b_id': {'type': 'string'},
          'team_a_player_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 5},
          'team_b_player_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 5},
          'week': {'type': 'integer', 'minimum': 1, 'maximum': 18}},
         ['team_a_id', 'team_b_id', 'team_a_player_ids', 'team_b_player_ids']),
]
NAMES = {definition['function']['name'] for definition in TOOLS}
_SCHEMAS = {definition['function']['name']: definition['function']['parameters'] for definition in TOOLS}
_STATES = frozenset(('PENDING', 'PROPOSED', 'ACCEPTED', 'EXECUTED', 'COMPLETE',
                     'COMPLETED', 'CANCELED', 'CANCELLED', 'DECLINED', 'REJECTED',
                     'VETOED', 'EXPIRED', 'FAILED', 'ERROR'))
_ITEM_TYPES = frozenset(('ADD', 'DROP', 'TRADE', 'MOVE', 'KEEPER'))
_CLOSED = frozenset(('CANCELED', 'CANCELLED', 'DECLINED', 'REJECTED', 'VETOED', 'EXPIRED', 'FAILED', 'ERROR'))
_SCOPE = 'Only records visible to the configured ESPN account; this is not a league-wide inventory of private offers.'


def _now():
    return datetime.now(timezone.utc)


def _id(value):
    if isinstance(value, bool):
        return None
    text = str(value)
    return text if re.fullmatch(r'-?\d{1,12}', text) else None


def _date(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def _teams(league):
    return {str(team.team_id): team for team in getattr(league, 'teams', [])}


def _player_name(league, pid):
    # Resolve names from the authenticated league snapshot, never arbitrary
    # transaction comments, member identities, bid messages or user text.
    for team in getattr(league, 'teams', []):
        for player in getattr(team, 'roster', []):
            if str(getattr(player, 'playerId', '')) == pid:
                return str(getattr(player, 'name', 'Unknown player'))[:100]
    value = getattr(league, 'player_map', {}).get(int(pid)) if pid is not None else None
    return value[:100] if isinstance(value, str) else None


def _player(league, pid):
    result = {'player_id': pid}
    name = _player_name(league, pid)
    if name:
        result['player_name'] = name
    return result


def _fingerprint(date, items):
    canonical = sorted((str(row.get('player_id')), str(row.get('from_team_id')),
                        str(row.get('to_team_id')), str(row.get('type'))) for row in items)
    return hashlib.sha256(json.dumps([date, canonical], sort_keys=True).encode()).hexdigest()[:20]


def _read(researcher, view, *, week=None, filters=None, timeout=8):
    from gamedaybot.espn.espn_read import read_league
    cache = researcher.__dict__.setdefault('transaction_cache', {})
    key = (view, week, json.dumps(filters, sort_keys=True))
    if key not in cache:
        if time.monotonic() >= researcher.deadline:
            raise TimeoutError('Transaction research deadline reached')
        if view == 'kona_league_communication':
            data = read_league(researcher.league, [view], path='/communication/', filters=filters,
                               deadline=researcher.deadline, timeout=timeout, max_bytes=1_000_000)
        else:
            params = {'scoringPeriodId': week} if week is not None else None
            data = read_league(researcher.league, [view], params=params, filters=filters,
                               deadline=researcher.deadline, timeout=timeout, max_bytes=1_000_000)
        cache[key] = data
    return cache[key]


def _matches(row, team_id):
    return team_id is None or team_id in row.get('team_ids', [])


def _items(league, raw):
    rows, known = [], _teams(league)
    if not isinstance(raw, list):
        return rows
    for entry in raw[:20]:
        if not isinstance(entry, dict) or entry.get('type') not in _ITEM_TYPES:
            continue
        pid = _id(entry.get('playerId'))
        if pid is None:
            continue
        source, target = _id(entry.get('fromTeamId')), _id(entry.get('toTeamId'))
        rows.append({**_player(league, pid), 'type': entry['type'],
                     'from_team_id': source if source in known else None,
                     'to_team_id': target if target in known else None})
    return rows


def _pending(researcher, args, *, timeout=8):
    data = _read(researcher, 'mPendingTransactions', week=researcher.league.scoringPeriodId, timeout=timeout)
    fetched = _now().isoformat()
    raw = data.get('pendingTransactions')
    if not isinstance(raw, list):
        return {'source': 'ESPN mPendingTransactions', 'fetched_at': fetched,
                'status': 'not_exposed', 'visibility': _SCOPE, 'trades': [],
                'visible_count': None, 'limitations': 'ESPN omitted the pending-trade collection. No visible offers is not proof of no pending trades.'}
    rows = []
    for row in raw[:100]:
        if not isinstance(row, dict):
            continue
        raw_type = row.get('type')
        items = _items(researcher.league, row.get('items'))
        if raw_type not in ('TRADE', 'TRADE_PROPOSAL', 'TRADE_ACCEPT', 'TRADE_ACCEPTED') and not any(item['type'] == 'TRADE' for item in items):
            continue
        status = row.get('status') if row.get('status') in _STATES else 'UNKNOWN'
        if status in _CLOSED:
            state = 'closed_without_verified_completion'
        elif _date(row.get('acceptedDate')) or status == 'ACCEPTED' or raw_type in ('TRADE_ACCEPT', 'TRADE_ACCEPTED'):
            state = 'accepted_awaiting_processing'
        elif status == 'PROPOSED' or raw_type == 'TRADE_PROPOSAL':
            state = 'proposed'
        else:
            state = 'pending_state_unconfirmed'
        when = _date(row.get('proposedDate'))
        entry = {'id': _fingerprint(when or row.get('id'), items), 'state': state,
                 'source_status': status, 'completed': False,
                 'is_pending': row.get('isPending') if isinstance(row.get('isPending'), bool) else None,
                 'proposed_at': when, 'accepted_at': _date(row.get('acceptedDate')),
                 'expires_at': _date(row.get('expirationDate')), 'items': items,
                 'team_ids': sorted({value for item in items for key in ('from_team_id', 'to_team_id') if (value := item[key]) is not None}),
                 'items_complete': isinstance(row.get('items'), list) and 0 < len(items) == len(row['items']) <= 20}
        initiator = _id(row.get('teamId'))
        if initiator in _teams(researcher.league) and initiator not in entry['team_ids']:
            entry['team_ids'].append(initiator)
        if _matches(entry, args.get('team_id')):
            rows.append(entry)
    rows.sort(key=lambda row: row.get('accepted_at') or row.get('proposed_at') or '', reverse=True)
    limit = args.get('limit', 10)
    return {'source': 'ESPN mPendingTransactions', 'fetched_at': fetched,
            'status': 'visible_records' if rows else 'no_visible_trade_records',
            'visibility': _SCOPE, 'trades': rows[:limit], 'visible_count': len(rows),
            'truncated': len(rows) > limit or len(raw) > 100,
            'limitations': 'Pending/proposed/accepted trades have not been verified as completed. No pending waiver claims, bids, comments or member/account identifiers are included. Missing player legs remain unknown.'}


def _completed(researcher, args, *, pages=4, timeout=8):
    cutoff = _now() - timedelta(days=args.get('days', 14))
    rows, seen, reached_end = [], set(), False
    for page in range(pages):
        filters = {'topics': {'filterType': {'value': ['ACTIVITY_TRANSACTIONS']},
                   'filterIncludeMessageTypeIds': {'value': [244]}, 'limit': 25,
                   'limitPerMessageSet': {'value': 25}, 'offset': page * 25,
                   'sortMessageDate': {'sortPriority': 1, 'sortAsc': False},
                   'sortFor': {'sortPriority': 2, 'sortAsc': False}}}
        data = _read(researcher, 'kona_league_communication', filters=filters, timeout=timeout)
        topics = data.get('topics')
        if not isinstance(topics, list):
            return {'error': 'Completed-trade activity is unavailable; do not infer no trades.', 'trades': []}
        older = False
        for topic in topics[:25]:
            if not isinstance(topic, dict):
                continue
            when = _date(topic.get('date'))
            if when is None:
                continue
            stamp = datetime.fromisoformat(when)
            if stamp < cutoff:
                older = True
                continue
            if stamp > _now() + timedelta(minutes=5):
                continue
            items, known = [], _teams(researcher.league)
            messages = topic.get('messages')
            if not isinstance(messages, list):
                continue
            for message in messages[:40]:
                if not isinstance(message, dict) or message.get('messageTypeId') != 244:
                    continue
                pid = _id(message.get('targetId'))
                source, target = _id(message.get('from')), _id(message.get('to'))
                if pid is None:
                    continue
                items.append({**_player(researcher.league, pid), 'type': 'TRADE',
                              'from_team_id': source if source in known else None,
                              'to_team_id': target if target in known else None})
            if not items:
                continue
            tid = _fingerprint(when, items)
            entry = {'id': tid, 'state': 'completed', 'completed': True, 'completed_at': when,
                     'items': items, 'team_ids': sorted({value for item in items for key in ('from_team_id', 'to_team_id') if (value := item[key]) is not None}),
                     'items_complete': len(messages) <= 40,
                     'directions_complete': len(messages) <= 40 and all(item['from_team_id'] is not None and item['to_team_id'] is not None for item in items)}
            if tid not in seen and _matches(entry, args.get('team_id')):
                seen.add(tid)
                rows.append(entry)
        if len(topics) < 25 or older:
            reached_end = True
            break
        if len(rows) >= args.get('limit', 10):
            break
    rows.sort(key=lambda row: row['completed_at'], reverse=True)
    limit = args.get('limit', 10)
    return {'source': 'ESPN completed-trade activity', 'fetched_at': _now().isoformat(),
            'since': cutoff.isoformat(), 'trades': rows[:limit],
            'scan_complete': reached_end, 'truncated': len(rows) > limit or not reached_end,
            'limitations': 'Recent visible activity only, capped at 100 topics. Completed transfer directions come from ESPN message type 244. An omitted or capped record is not proof a trade never occurred.'}


def _history(researcher, args):
    week = args.get('week', researcher.week)
    types = sorted(set(args.get('types', ('FREEAGENT', 'WAIVER', 'TRADE_ACCEPT'))))
    data = _read(researcher, 'mTransactions2', week=week,
                 filters={'transactions': {'filterType': {'value': types}}})
    raw = data.get('transactions')
    if not isinstance(raw, list):
        return {'error': 'ESPN did not expose transaction records for this week; missing records are unknown.', 'week': week, 'transactions': []}
    rows, known = [], _teams(researcher.league)
    for row in raw[:200]:
        if not isinstance(row, dict) or row.get('type') not in types:
            continue
        if row.get('scoringPeriodId') is not None and row['scoringPeriodId'] != week:
            continue
        raw_status = row.get('status') if row.get('status') in _STATES else 'UNKNOWN'
        # Never surface pending waiver claims or failed bids from a private view.
        if row['type'] in ('WAIVER', 'FREEAGENT') and (row.get('isPending') is True or raw_status not in ('EXECUTED', 'COMPLETE', 'COMPLETED')):
            continue
        items = _items(researcher.league, row.get('items'))
        team = _id(row.get('teamId'))
        team_ids = {value for item in items for key in ('from_team_id', 'to_team_id') if (value := item[key]) is not None}
        if team in known:
            team_ids.add(team)
        date = _date(row.get('processDate')) or _date(row.get('proposedDate'))
        entry = {'id': _fingerprint(row.get('id') or date, items), 'type': row['type'],
                 'source_status': raw_status, 'scoring_week': week,
                 'processed_at': _date(row.get('processDate')), 'proposed_at': _date(row.get('proposedDate')),
                 'team_ids': sorted(team_ids), 'items': items,
                 'items_complete': isinstance(row.get('items'), list) and 0 < len(items) == len(row['items']) <= 20}
        if row['type'].startswith('TRADE_'):
            entry['trade_completion'] = 'Not established by this lifecycle event. Use get_recent_trades for completed transfers.'
        if _matches(entry, args.get('team_id')):
            rows.append(entry)
    rows.sort(key=lambda row: row['processed_at'] or row['proposed_at'] or '', reverse=True)
    limit = args.get('limit', 10)
    return {'source': 'ESPN mTransactions2', 'fetched_at': _now().isoformat(), 'week': week,
            'visibility': _SCOPE, 'transactions': rows[:limit],
            'truncated': len(rows) > limit or len(raw) > 200,
            'limitations': 'TRADE_ACCEPT is an acceptance event, not confirmation of a completed exchange. ESPN can omit trade player legs for other managers. Current ownership cannot reconstruct missing historical transfers. Bid amounts, private claims, comments and account identifiers are excluded.'}


def _proposal(researcher, args):
    teams = _teams(researcher.league)
    team_a, team_b = teams[args['team_a_id']], teams[args['team_b_id']]
    if team_a is team_b:
        return {'error': 'A trade requires two different fantasy teams.'}
    a_ids, b_ids = set(args['team_a_player_ids']), set(args['team_b_player_ids'])
    a_players = {str(player.playerId): player for player in team_a.roster}
    b_players = {str(player.playerId): player for player in team_b.roster}
    if (len(team_a.roster) > 40 or len(team_b.roster) > 40 or a_ids & b_ids
            or not a_ids <= a_players.keys() or not b_ids <= b_players.keys()
            or len(a_players) != len(team_a.roster) or len(b_players) != len(team_b.roster)):
        return {'error': 'Each offered player must be uniquely owned by its stated sending team in the current roster snapshot.'}
    week = args.get('week', researcher.week)
    slots = researcher.context.get('league_rules', {}).get('lineup_slots')
    if (not isinstance(slots, dict) or len(slots) > 25
            or any(not isinstance(k, str) or type(v) is not int or not 0 <= v <= 40 for k, v in slots.items())):
        slots = None
    rows = []
    for team, source, outgoing, incoming in ((team_a, a_players, a_ids, [b_players[p] for p in sorted(b_ids)]),
                                              (team_b, b_players, b_ids, [a_players[p] for p in sorted(a_ids)])):
        after_roster = [player for pid, player in source.items() if pid not in outgoing] + incoming
        before, after = lineup(team.roster, slots, week), lineup(after_roster, slots, week)
        rows.append({'team_id': str(team.team_id), 'sent_player_ids': sorted(outgoing),
                     'received_player_ids': [str(player.playerId) for player in incoming],
                     'before': before, 'after': after,
                     'starter_projection_change': round(after['projected_points'] - before['projected_points'], 2)
                         if before.get('available') and after.get('available') else None,
                     'depth_before': depth(team.roster), 'depth_after': depth(after_roster)})
    return {'source': 'Current ESPN rosters and supplied verified lineup rules', 'kind': 'hypothetical_trade',
            'week': week, 'completed': False, 'teams': rows,
            'limitations': 'This simulates only the specified exchange; it is not evidence an offer exists or has been accepted. Uses current rosters and the requested week\'s available ESPN projections, not rest-of-season values. Locked lineups, roster caps, transaction timing, future moves and legal eligibility to submit a trade are not simulated. Missing projections prevent a numerical gain estimate. No ESPN write operations are performed.'}


def _validate(name, args, researcher):
    schema = _SCHEMAS[name]
    if not isinstance(args, dict) or set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        return False
    for key, value in args.items():
        if key in ('limit', 'days', 'week'):
            upper = {'limit': 20, 'days': 60, 'week': 18}[key]
            if type(value) is not int or not 1 <= value <= upper:
                return False
        elif key in ('team_id', 'team_a_id', 'team_b_id'):
            if not isinstance(value, str) or value not in _teams(researcher.league):
                return False
        elif key in ('team_a_player_ids', 'team_b_player_ids'):
            if (not isinstance(value, list) or not 1 <= len(value) <= 5 or
                    any(not isinstance(pid, str) or _id(pid) is None for pid in value) or len(set(value)) != len(value)):
                return False
        elif key == 'types':
            if not isinstance(value, list) or not 1 <= len(value) <= 6 or any(not isinstance(item, str) or item not in HISTORY_TYPES for item in value):
                return False
    current = getattr(researcher.league, 'scoringPeriodId', None)
    week = args.get('week', researcher.week)
    if name in ('evaluate_trade_proposal', 'get_transaction_history'):
        if type(week) is not int or not 1 <= week <= 18 or type(current) is not int:
            return False
        if name == 'get_transaction_history' and week > current:
            return False
        if name == 'evaluate_trade_proposal' and not current <= week <= min(current + 3, 18):
            return False
    return True


def execute(name, args, researcher):
    if name not in NAMES or not _validate(name, args, researcher):
        return {'error': 'Use only the documented transaction arguments, known team IDs and bounded week/record limits.'}
    if time.monotonic() > researcher.deadline - 20:
        return {'error': 'Research budget exhausted.'}
    if researcher.context.get('historical') and name != 'get_transaction_history':
        return {'error': 'Current offers, transactions and current-roster trade simulations are excluded from historical reports.'}
    if researcher.context.get('historical') and args.get('week', researcher.week) > researcher.week:
        return {'error': 'Transaction history cannot extend beyond the historical report week.'}
    try:
        return {'get_recent_trades': _completed, 'get_pending_trades': _pending,
                'get_transaction_history': _history, 'evaluate_trade_proposal': _proposal}[name](researcher, args)
    except Exception:
        # Upstream errors can contain URLs, cookie details or raw private JSON.
        return {'error': 'ESPN transaction research is unavailable; missing information is unknown.'}


def build_trade_awareness(league, deadline=None, cache=None):
    """A small automatic snapshot, sharing a strict eight-second fetch budget.

    The optional cache belongs to this report request, never another account or
    an unbounded global store. No results are written to the trade-notifier's
    deduplication ledger: only that separate delivery flow marks trades sent.
    """
    deadline = min(deadline, time.monotonic() + 8) if deadline is not None else time.monotonic() + 8
    researcher = SimpleNamespace(league=league, context={}, week=getattr(league, 'scoringPeriodId', None),
                                 deadline=deadline, transaction_cache=cache if cache is not None else {})
    result = {'observed_at': _now().isoformat(), 'visibility': _SCOPE,
              'limitations': 'Pending and accepted offers are not completed trades. Only completed activity verifies transfers. This is a capped snapshot; use transaction tools for details. Unavailable or omitted records are unknown.'}
    for key, fetch in (('pending', _pending), ('recent_completed', _completed)):
        try:
            kwargs = {'pages': 1} if key == 'recent_completed' else {}
            fetched = fetch(researcher, {'limit': 3, 'days': 7}, timeout=3.5, **kwargs)
            result[key] = {field: value for field, value in fetched.items()
                           if field not in ('source', 'visibility', 'limitations')}
            for trade in result[key].get('trades', []):
                if len(trade['items']) > 6:
                    trade['omitted_items'] = len(trade['items']) - 6
                    trade['items'] = trade['items'][:6]
                    trade['items_complete'] = False
        except Exception:
            result[key] = {'status': 'unavailable', 'trades': [],
                           'limitations': 'Source unavailable; do not infer no pending or completed trades.'}
    return result
