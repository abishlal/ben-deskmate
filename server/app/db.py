"""SQLite storage: reminders, todos, notes, memory, outbox and daily usage."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta

from . import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, due_at INTEGER NOT NULL,
        repeat TEXT NOT NULL DEFAULT 'none', status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS todos(
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
        due_at INTEGER, priority TEXT NOT NULL DEFAULT 'normal', done INTEGER NOT NULL DEFAULT 0,
        nudged INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, completed_at INTEGER);
    CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY, fact TEXT NOT NULL, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
        created INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, turns INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
    CREATE TABLE IF NOT EXISTS ms_nudged(key TEXT PRIMARY KEY, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    -- Work-log lines waiting to be written to OneNote (OneNote holds the log itself).
    CREATE TABLE IF NOT EXISTS worklog_queue(id INTEGER PRIMARY KEY, month TEXT NOT NULL, line TEXT NOT NULL, created INTEGER NOT NULL);
    -- One short summary per day of conversation ('' = nothing worth summarising that day).
    CREATE TABLE IF NOT EXISTS day_summaries(day TEXT PRIMARY KEY, summary TEXT NOT NULL, created INTEGER NOT NULL);
    """
)
# Full-text index over the chat history, kept in step with the messages table by a trigger.
try:
    _conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS messages_fts_add AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_del AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        END;
        """
    )
    if not _conn.execute("SELECT 1 FROM messages_fts LIMIT 1").fetchone():
        _conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    HAS_FTS = True
except sqlite3.OperationalError:
    HAS_FTS = False
# Columns added after the first release.
for _table, _column in (("outbox", "delivered_at INTEGER"),):
    try:
        _conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_column}")
    except sqlite3.OperationalError:
        pass  # already there


def run(sql: str, args: tuple = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    found = rows(sql, args)
    return found[0] if found else None


def ts() -> int:
    return int(time.time())


def fmt(epoch: int | None) -> str:
    if not epoch:
        return "no date"
    return datetime.fromtimestamp(epoch, config.TZ).strftime("%a %d %b %Y %H:%M")


def parse_when(value: str) -> int:
    """ISO 8601 date-time; a value without an offset is taken as local time."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return int(dt.timestamp())


def next_occurrence(due_at: int, repeat: str) -> int | None:
    """The next due time after now for a repeating reminder, or None if it does not repeat."""
    if repeat == "none":
        return None
    dt = datetime.fromtimestamp(due_at, config.TZ)
    now = config.now()
    while True:
        dt += timedelta(days=7 if repeat == "weekly" else 1)
        if repeat == "weekdays" and dt.weekday() >= 5:
            continue
        if dt > now:
            return int(dt.timestamp())


# --- usage -----------------------------------------------------------------

def usage_today() -> dict:
    day = config.now().strftime("%Y-%m-%d")
    return one("SELECT * FROM usage WHERE day=?", (day,)) or {"day": day, "turns": 0, "cost": 0.0}


def usage_days(days: int) -> list[dict]:
    """Turns and cost per day for the last few days, newest first."""
    since = (config.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return rows("SELECT day, turns, cost FROM usage WHERE day>=? ORDER BY day DESC", (since,))


def add_usage(cost: float) -> None:
    day = config.now().strftime("%Y-%m-%d")
    run(
        "INSERT INTO usage(day, turns, cost) VALUES(?, 1, ?) "
        "ON CONFLICT(day) DO UPDATE SET turns = turns + 1, cost = cost + excluded.cost",
        (day, cost),
    )


# --- chat history -------------------------------------------------------------
# role is "user", "pet" (a reply) or "announce" (something the pet said unprompted).

def log_message(role: str, text: str) -> None:
    if text.strip():
        run("INSERT INTO messages(ts, role, text) VALUES(?,?,?)", (ts(), role, text.strip()))


def recent_messages(limit: int, max_age_s: int) -> list[dict]:
    found = rows("SELECT ts, role, text FROM messages WHERE ts>=? ORDER BY id DESC LIMIT ?", (ts() - max_age_s, limit))
    return found[::-1]


def kv_get(key: str) -> str | None:
    row = one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    run("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
