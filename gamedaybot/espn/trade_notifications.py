"""Durable, at-most-once automatic trade announcements."""

import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import time

from gamedaybot.espn.trades import completed_trades, format_trade
from gamedaybot.espn.analysis import generate_analysis

logger = logging.getLogger(__name__)


class TradeLedger:
    def __init__(self, path=None):
        path = path or os.environ.get('TRADE_STATE_PATH', 'data/trades.sqlite3')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS watches (scope TEXT PRIMARY KEY, since_ms INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS deliveries (scope TEXT, trade_id TEXT, status TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(scope,trade_id))')

    def start(self, scope, now_ms=None):
        self.db.execute('INSERT OR IGNORE INTO watches VALUES (?,?)', (scope, now_ms if now_ms is not None else int(time.time()*1000)))
        return self.db.execute('SELECT since_ms FROM watches WHERE scope=?', (scope,)).fetchone()[0]

    def known(self, scope, key):
        return self.db.execute('SELECT status FROM deliveries WHERE scope=? AND trade_id=?', (scope,key)).fetchone() is not None

    def claim(self, scope, key):
        return self.db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)', (scope,key,'claimed',time.time())).rowcount == 1

    def finish(self, scope, key, status):
        self.db.execute('UPDATE deliveries SET status=?,updated=? WHERE scope=? AND trade_id=?', (status,time.time(),scope,key))

    def close(self):
        self.db.close()


def poll_trades(league, data, discord):
    url = str(data.get('discord_webhook_url', ''))
    if url in ('', '1'):
        return
    scope = f"{data['league_id']}:{data.get('year',2026)}:" + hashlib.sha256(url.encode()).hexdigest()
    ledger = TradeLedger()
    try:
        since = ledger.start(scope)
        trades = completed_trades(league, since)
        for trade in trades:
            if ledger.known(scope, trade['id']):
                continue
            text = format_trade(trade, data['my_timezone'])
            analysis = generate_analysis(text, 'get_trade_report', timezone=data['my_timezone'],
                                         week=getattr(league,'scoringPeriodId',None), league=league,
                                         trade_actions=trade['actions'])
            if analysis:
                text += '\n\n' + analysis
            # Reserve before sending: webhooks cannot guarantee exactly-once
            # delivery after a timeout. Never retry an ambiguous send automatically.
            if not ledger.claim(scope, trade['id']):
                continue
            discord.teams = getattr(league, 'teams', [])
            try:
                discord.send_message(text)
            except Exception as error:
                ledger.finish(scope, trade['id'], 'uncertain')
                logger.error('Trade notification delivery uncertain (%s); automatic retry suppressed', type(error).__name__)
                continue
            ledger.finish(scope, trade['id'], 'sent')
        logger.info('Hourly trade scan complete')
    finally:
        ledger.close()
