"""Bounded, designation-only changes from ESPN's current fantasy rosters.

First observations establish a baseline. A designation describes ESPN's roster
status, not a diagnosis, prognosis, confirmed inactivity or proof of full health.
The caller persists the returned state only as part of its delivery workflow.
"""
from datetime import datetime, timezone
from itertools import islice
import math
import re


MAX_PLAYERS = 2048
MAX_TEAMS = 64
MAX_ROSTER = 128
_KNOWN = {
    'ACTIVE', 'QUESTIONABLE', 'DOUBTFUL', 'OUT', 'PROBABLE', 'DAY_TO_DAY',
    'INJURY_RESERVE', 'SUSPENSION', 'PUP', 'PHYSICALLY_UNABLE_TO_PERFORM',
    'NON_FOOTBALL_INJURY', 'NON_FOOTBALL_ILLNESS',
}
_ALIASES = {
    'NORMAL': 'ACTIVE', 'IR': 'INJURY_RESERVE', 'INJURED_RESERVE': 'INJURY_RESERVE',
    'SUSPENDED': 'SUSPENSION', 'DTD': 'DAY_TO_DAY',
    'PHYSICALLY_UNABLE_TO_PERFORM': 'PUP',
}
_LIMITATION = ('Observed ESPN designation change, not the injury onset time. '
               'ACTIVE means the designation was removed; it does not establish full health '
               'or participation. Other designations are not a medical prognosis.')


def _status(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    normalized = re.sub(r'[\s-]+', '_', value.strip().upper())
    normalized = _ALIASES.get(normalized, normalized)
    return normalized if normalized in _KNOWN else None


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    if not re.fullmatch(r'-?\d{1,20}', value) or int(value) == 0:
        return None
    return str(int(value))


def _text(value, fallback='', limit=200):
    return ' '.join(value.split())[:limit] if isinstance(value, str) and value.strip() else fallback


def _timestamp(value=None):
    if value is None:
        stamp = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
        stamp = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('observed_at must be a datetime, ISO timestamp or Unix seconds')
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def _revision(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _previous(previous):
    """Copy only the small, validated fields this collector owns."""
    if not isinstance(previous, dict) or previous.get('version') != 1:
        return {}, 0
    raw = previous.get('players', {})
    if not isinstance(raw, dict):
        return {}, _revision(previous.get('revision_seed'))
    players = {}
    for player_id, row in islice(raw.items(), MAX_PLAYERS):
        player_id = _id(player_id)
        if not player_id or not isinstance(row, dict):
            continue
        status = _status(row.get('status'))
        if status is None:
            continue
        try:
            last_seen = _timestamp(row.get('last_seen')) if row.get('last_seen') else ''
        except (ValueError, TypeError, OverflowError, OSError):
            last_seen = ''
        players[player_id] = {'status': status, 'revision': _revision(row.get('revision')),
                              'last_seen': last_seen}
    seed = max([_revision(previous.get('revision_seed')),
                *(row['revision'] for row in players.values())])
    return players, seed


def collect_injuries(league, previous=None, observed_at=None):
    """Return JSON-safe status-change events and a bounded next snapshot.

    Track all roster slots, including bench and IR. Missing/unknown status keeps
    the last known designation; first-seen players are silently baselined even
    after initialization. Ownership and slot changes alone never produce events.
    Dropped players remain in a most-recently-seen cache, with rostered players
    taking priority when the 2,048-record limit is reached. A revision high-water
    mark prevents event IDs repeating if an evicted player is later re-added.
    """
    stamp = _timestamp(observed_at)
    players, seed = _previous(previous)
    observations, conflicts = {}, set()
    teams = getattr(league, 'teams', None)
    for team in teams[:MAX_TEAMS] if isinstance(teams, (list, tuple)) else []:
        team_id = _id(getattr(team, 'team_id', None))
        roster = getattr(team, 'roster', None)
        if not team_id or not isinstance(roster, (list, tuple)):
            continue
        for player in roster[:MAX_ROSTER]:
            player_id = _id(getattr(player, 'playerId', None))
            if not player_id:
                continue
            row = {'player_id': player_id, 'team_id': team_id,
                   'player_name': _text(getattr(player, 'name', None), 'Player ' + player_id),
                   'team': _text(getattr(team, 'team_name', None), 'Team ' + team_id),
                   'status': _status(getattr(player, 'injuryStatus', None))}
            for key, attributes in (('position', ('position',)), ('slot', ('slot_position', 'lineupSlot'))):
                value = next((_text(getattr(player, attr, None), limit=40)
                              for attr in attributes if _text(getattr(player, attr, None))), '')
                if value:
                    row[key] = value
            if player_id in observations and observations[player_id] != row:
                conflicts.add(player_id)
            if player_id in observations or len(observations) < MAX_PLAYERS:
                observations[player_id] = row

    events = []
    for player_id, row in observations.items():
        old = players.get(player_id)
        if old is not None:
            old['last_seen'] = stamp
        if player_id in conflicts or row['status'] is None:
            continue
        if old is None:
            players[player_id] = {'status': row['status'], 'revision': seed, 'last_seen': stamp}
        elif old['status'] != row['status']:
            revision = old['revision'] + 1
            events.append({**row, 'id': f'injury:{player_id}:{revision}', 'kind': 'injury_status',
                           'observed_at': stamp, 'previous_status': old['status'],
                           'limitation': _LIMITATION})
            old.update(status=row['status'], revision=revision)
    seed = max([seed, *(row['revision'] for row in players.values())])
    retained = sorted(players, key=lambda pid: (pid in observations, players[pid]['last_seen'], pid), reverse=True)
    return events, {'version': 1, 'observed_at': stamp, 'revision_seed': seed,
                    'players': {pid: players[pid] for pid in retained[:MAX_PLAYERS]}}
