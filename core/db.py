"""SQLite storage. Every process/thread opens its own connection (WAL mode)."""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  password_enc TEXT NOT NULL,
  added_at REAL NOT NULL,
  cap_override INTEGER,
  paused_until REAL,
  disabled_reason TEXT,
  last_error TEXT,
  next_send_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',      -- draft, running, paused, stopped, done
  status_reason TEXT,
  source_type TEXT NOT NULL,                 -- xlsx, csv, gsheet
  source_ref TEXT NOT NULL,                  -- server file path or sheet URL
  original_name TEXT,
  tab TEXT,
  header_row INTEGER,
  mapping TEXT,                              -- JSON {field: col}
  notes_col INTEGER,
  notes_header_missing INTEGER DEFAULT 0,
  skip_sent INTEGER DEFAULT 1,
  subject TEXT DEFAULT '',
  body TEXT DEFAULT '',
  dry_run INTEGER DEFAULT 0,
  started_at REAL,
  finished_at REAL,
  ack_bounces INTEGER DEFAULT 0,
  ack_unsubs INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipients(
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL,
  row_num INTEGER NOT NULL,
  email TEXT,
  raw_email TEXT,
  name TEXT,
  first_name TEXT,
  last_name TEXT,
  position TEXT,
  status TEXT NOT NULL,                      -- queued, sending, sent, failed, skipped
  skip_reason TEXT,
  suggestion TEXT,
  account_id INTEGER,
  message_id TEXT,
  attempts INTEGER DEFAULT 0,
  next_attempt_at REAL DEFAULT 0,
  sending_at REAL,
  sent_at REAL,
  error TEXT,
  bounced_at REAL,
  replied_at REAL,
  unsubscribed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_rcpt_queue ON recipients(campaign_id, status, row_num);
CREATE INDEX IF NOT EXISTS ix_rcpt_email ON recipients(email);
CREATE INDEX IF NOT EXISTS ix_rcpt_msgid ON recipients(message_id);

CREATE TABLE IF NOT EXISTS send_log(
  id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, recipient_id INTEGER, ts REAL NOT NULL, kind TEXT
);
CREATE INDEX IF NOT EXISTS ix_sendlog ON send_log(account_id, ts);

CREATE TABLE IF NOT EXISTS suppression(
  email TEXT PRIMARY KEY, reason TEXT NOT NULL, added_at REAL NOT NULL, note TEXT
);

CREATE TABLE IF NOT EXISTS marks(
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL,
  recipient_id INTEGER,
  row_num INTEGER NOT NULL,
  email TEXT,
  note TEXT NOT NULL,
  created_at REAL NOT NULL,
  written_at REAL,
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  unmatched INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_marks_pending ON marks(campaign_id, written_at);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL,
  campaign_id INTEGER, account_id INTEGER, message TEXT NOT NULL, dismissed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS imap_state(
  account_id INTEGER PRIMARY KEY, uidvalidity TEXT, last_uid INTEGER DEFAULT 0, last_check REAL
);

CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT, ts REAL);
"""

DEFAULT_SETTINGS = {
    "window": {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "16:00"},
    "warmup": [20, 40, 60, 80],
    "test_inbox": "",
}


def connect(path=None) -> sqlite3.Connection:
    path = path or (config.data_dir() / "outreach.db")
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_SETTINGS.get(key)))
    return json.loads(row["value"])


def set_setting(conn, key, value) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def add_event(conn, level: str, message: str, campaign_id=None, account_id=None) -> None:
    conn.execute("INSERT INTO events(ts,level,campaign_id,account_id,message) VALUES(?,?,?,?,?)",
                 (time.time(), level, campaign_id, account_id, message))


def suppression_reason(conn, email: str):
    row = conn.execute("SELECT reason FROM suppression WHERE email=?", (email,)).fetchone()
    return row["reason"] if row else None


def suppress(conn, email: str, reason: str, note: str = "") -> None:
    """Add to the global suppression list. 'bounced' and 'unsubscribed' win over 'replied'."""
    rank = {"replied": 1, "manual": 2, "unsubscribed": 3, "bounced": 3}
    cur = suppression_reason(conn, email)
    if cur is None:
        conn.execute("INSERT INTO suppression(email,reason,added_at,note) VALUES(?,?,?,?)",
                     (email, reason, time.time(), note))
    elif rank.get(reason, 0) > rank.get(cur, 0):
        conn.execute("UPDATE suppression SET reason=?, note=? WHERE email=?", (reason, note, email))
