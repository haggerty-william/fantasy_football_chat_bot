"""Persistent preferences, picks and notification reservations on the existing PVC."""
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def state():
    path = os.environ.get('COMMUNITY_STATE_PATH') or str(Path(os.environ.get('TRADE_STATE_PATH', 'data/trades.sqlite3')).with_name('community.sqlite3'))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS subscriptions(scope TEXT,user TEXT,team INTEGER,PRIMARY KEY(scope,user));
    CREATE TABLE IF NOT EXISTS picks(scope TEXT,week INTEGER,user TEXT,name TEXT,game TEXT,team INTEGER,result REAL,PRIMARY KEY(scope,week,user,game));
    CREATE TABLE IF NOT EXISTS notices(scope TEXT,key TEXT,PRIMARY KEY(scope,key));
    ''')
    try:
        yield db
        db.commit()
    finally:
        db.close()


def scope(league, guild=None):
    return f"{guild or os.environ.get('DISCORD_GUILD_ID', 'local')}:{league.league_id}:{league.year}"


def reserve(scope_id, key):
    with state() as db:
        return db.execute('INSERT OR IGNORE INTO notices VALUES (?,?)', (scope_id,key)).rowcount == 1
