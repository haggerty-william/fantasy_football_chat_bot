"""Hourly league changes with persistent observations and at-most-once delivery."""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from threading import Lock
import time
from zoneinfo import ZoneInfo

from gamedaybot.espn.analysis import generate_analysis
from gamedaybot.espn.analysis_limits import analysis_timeout
from gamedaybot.espn.injury_updates import collect_injuries
from gamedaybot.espn.transaction_updates import collect_proposals, collect_roster_moves
from gamedaybot.espn.trade_notifications import poll_trades


logger = logging.getLogger(__name__)
_poll_lock = Lock()
MAX_BATCH_EVENTS = 10
MAX_BATCH_CHARS = 10000


class UpdateLedger:
    """Source cursors and pending changes commit together before model work."""

    def __init__(self, path=None):
        path = path or os.environ.get('TRADE_STATE_PATH', 'data/trades.sqlite3')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS league_update_snapshots '
                        '(scope TEXT, source TEXT, data TEXT NOT NULL, PRIMARY KEY(scope,source))')
        self.db.execute('CREATE TABLE IF NOT EXISTS league_update_events '
                        '(scope TEXT, event_id TEXT, source TEXT, data TEXT NOT NULL, '
                        'status TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(scope,event_id))')
        self.db.commit()

    def snapshot(self, scope, source):
        row = self.db.execute('SELECT data FROM league_update_snapshots WHERE scope=? AND source=?',
                              (scope, source)).fetchone()
        return json.loads(row[0]) if row else None

    def observe(self, scope, source, events, snapshot):
        # Invalid source data must not partially advance its cursor.
        encoded = [(event['id'], json.dumps(event, ensure_ascii=False)) for event in events]
        state = json.dumps(snapshot, ensure_ascii=False)
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO league_update_events VALUES (?,?,?,?,?,?)',
                [(scope, eid, source, data, 'pending', time.time()) for eid, data in encoded])
            self.db.execute('INSERT INTO league_update_snapshots VALUES (?,?,?) '
                            'ON CONFLICT(scope,source) DO UPDATE SET data=excluded.data', (scope, source, state))

    def pending(self, scope):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT data FROM league_update_events WHERE scope=? AND status=? ORDER BY rowid',
            (scope, 'pending'))]

    def claim(self, scope, events):
        # Reserve immediately before sending. An ambiguous HTTP result must not
        # trigger a duplicate report after restart or on the next hourly scan.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for event in events:
                row = self.db.execute('SELECT status FROM league_update_events WHERE scope=? AND event_id=?',
                                      (scope, event['id'])).fetchone()
                if row != ('pending',):
                    return False
            self.db.executemany('UPDATE league_update_events SET status=?,updated=? WHERE scope=? AND event_id=?',
                                [('claimed', time.time(), scope, event['id']) for event in events])
        return True

    def finish(self, scope, events, status):
        with self.db:
            self.db.executemany('UPDATE league_update_events SET status=?,updated=? WHERE scope=? AND event_id=?',
                                [(status, time.time(), scope, event['id']) for event in events])

    def close(self):
        self.db.close()


def _clean(value):
    return ' '.join(str(value or '').split())[:120]


def _team(league, team_id):
    team = next((t for t in league.teams if str(t.team_id) == str(team_id)), None)
    return _clean(getattr(team, 'team_name', None)) or 'Unknown team'


def _player(item):
    return _clean(item.get('player_name')) or 'Player #' + _clean(item.get('player_id'))


def _event_lines(event, league):
    if event['kind'] == 'injury_status':
        previous = _clean(event['previous_status']).replace('_', ' ').title()
        current = _clean(event['status']).replace('_', ' ').title()
        if event['status'] == 'ACTIVE':
            current = 'Active (designation removed; not a full-health confirmation)'
        return [f"{_team(league, event['team_id'])}: {_clean(event['player_name'])} — {previous} → {current}"]
    row = event['trade'] if event['kind'] == 'trade_proposal' else event['transaction']
    if event['kind'] == 'trade_proposal':
        state = row.get('state')
        status = {
            'proposed': 'Trade proposed — not completed',
            'accepted_awaiting_processing': 'Trade accepted — awaiting processing, not completed',
            'closed_without_verified_completion': 'Trade proposal closed — ' + _clean(row.get('source_status')).lower(),
        }.get(state, 'Pending trade — exact status unconfirmed; not completed')
        lines = [status + ':']
        received = {}
        for item in row.get('items', []):
            if item.get('type') != 'TRADE':
                continue
            received.setdefault(item.get('to_team_id'), []).append(_player(item))
        verb = 'would have received' if state == 'closed_without_verified_completion' else 'would receive'
        for tid, players in received.items():
            lines.append(f"{_team(league, tid)} {verb} {', '.join(players)}")
        if not row.get('items_complete') or not received:
            lines.append('ESPN did not expose all player movements; the offer cannot be fully evaluated.')
        return lines
    label = 'Waiver processed' if row.get('type') == 'WAIVER' else 'Roster transaction completed'
    lines = [label + ':']
    for item in row.get('items', []):
        if item.get('type') == 'ADD':
            lines.append(f"{_team(league, item.get('to_team_id'))} added {_player(item)}")
        elif item.get('type') == 'DROP':
            lines.append(f"{_team(league, item.get('from_team_id'))} dropped {_player(item)}")
    if not row.get('items_complete'):
        lines.append('Some transaction details are unavailable.')
    return lines


def format_updates(events, league, local_timezone='America/New_York'):
    """Batch readable facts by subject; do not imply observation time is event time."""
    lines = ['League activity', datetime.now(ZoneInfo(local_timezone)).strftime('Checked %b %d · %I:%M %p %Z')]
    for kind, title in (('trade_proposal', 'Trade proposal updates'), ('injury_status', 'Injury status changes'),
                        ('roster_move', 'Roster moves')):
        selected = [event for event in events if event['kind'] == kind]
        if not selected:
            continue
        lines += ['', title]
        for event in selected:
            lines.extend(_event_lines(event, league))
            lines.append('')
        if kind == 'trade_proposal':
            lines.append('Offers shown are limited to what the configured ESPN account can see.')
        if kind == 'injury_status':
            lines.append('Changes since the last successful check. ESPN does not supply the exact injury time.')
    return '\n'.join(lines).strip()


def _batches(events, league, local_timezone):
    batch = []
    for event in events:
        trial = batch + [event]
        if batch and (len(trial) > MAX_BATCH_EVENTS or len(json.dumps(trial)) > MAX_BATCH_CHARS
                      or len(format_updates(trial, league, local_timezone)) > MAX_BATCH_CHARS):
            yield batch
            batch = []
        batch.append(event)
    if batch:
        yield batch


def poll_league_updates(league, data, discord):
    """Quiet unchanged scans; source failures leave successful sources usable."""
    url = str(data.get('discord_webhook_url', ''))
    if url in ('', '1') or not _poll_lock.acquire(blocking=False):
        return
    ledger = None
    try:
        scope = f"{data['league_id']}:{data.get('year', 2026)}:" + hashlib.sha256(url.encode()).hexdigest()
        ledger = UpdateLedger()
        observed = datetime.now(timezone.utc).isoformat()
        for source, collect in (('injuries', collect_injuries), ('proposals', collect_proposals),
                                ('roster_moves', collect_roster_moves)):
            try:
                previous = ledger.snapshot(scope, source)
                events, snapshot = collect(league, previous, observed_at=observed)
                ledger.observe(scope, source, events, snapshot)
                logger.info('Hourly %s scan events=%s baseline=%s', source, len(events), previous is None)
            except Exception as error:
                logger.warning('Hourly %s source unavailable (%s); retaining prior observations', source, type(error).__name__)

        # Bound total model work across the scan, including completed trades.
        # A backlog still gets its factual reports when this budget is spent.
        analysis_deadline = time.monotonic() + 2 * analysis_timeout() + 1
        if data.get('trade_report', True):
            try:
                poll_trades(league, data, discord, analysis_deadline=analysis_deadline)
            except Exception as error:
                logger.warning('Hourly completed-trade source unavailable (%s)', type(error).__name__)

        pending = ledger.pending(scope)
        for events in _batches(pending, league, data['my_timezone']):
            report = format_updates(events, league, data['my_timezone'])
            analysis = ''
            try:
                if time.monotonic() + analysis_timeout() <= analysis_deadline:
                    analysis = generate_analysis(report, 'get_league_updates', timezone=data['my_timezone'],
                        week=getattr(league, 'scoringPeriodId', None), league=league, event_facts=events)
            except Exception as error:
                logger.warning('Hourly commentary unavailable (%s); delivering verified changes', type(error).__name__)
            if not ledger.claim(scope, events):
                continue
            discord.teams = getattr(league, 'teams', [])
            try:
                discord.send_message(report + ('\n\n' + analysis if analysis else ''))
            except Exception as error:
                ledger.finish(scope, events, 'uncertain')
                logger.warning('Hourly update delivery uncertain (%s); automatic retry suppressed', type(error).__name__)
            else:
                ledger.finish(scope, events, 'sent')
        logger.info('Hourly league scan complete')
    finally:
        if ledger is not None:
            ledger.close()
        _poll_lock.release()
