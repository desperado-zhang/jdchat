from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL DEFAULT 'jd_dongdong',
  conversation_key TEXT NOT NULL UNIQUE,
  vender_id TEXT,
  vender_name TEXT,
  seller_app TEXT,
  seller_pin_hash TEXT,
  customer_app TEXT,
  customer_pin_hash TEXT,
  customer_name TEXT,
  session_type TEXT,
  last_msg_id TEXT,
  last_mid INTEGER,
  last_message_at TEXT,
  unread_count INTEGER,
  last_read_mid INTEGER,
  raw_customer TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_vender
  ON conversations (vender_id);

CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at
  ON conversations (last_message_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dedupe_key TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL DEFAULT 'jd_dongdong',
  conversation_key TEXT NOT NULL,
  msg_id TEXT,
  mid INTEGER,
  local_id TEXT,
  direction TEXT NOT NULL,
  top_type TEXT,
  body_type TEXT,
  content TEXT,
  content_hash TEXT,
  media_url TEXT,
  media_width INTEGER,
  media_height INTEGER,
  media_size INTEGER,
  template_type TEXT,
  template_payload TEXT,
  message_at TEXT,
  client_time INTEGER,
  datetime_ms INTEGER,
  timestamp_ms INTEGER,
  read_flag INTEGER,
  state INTEGER,
  lang TEXT,
  from_app TEXT,
  from_pin_hash TEXT,
  from_client_type TEXT,
  from_art TEXT,
  to_app TEXT,
  to_pin_hash TEXT,
  source TEXT NOT NULL,
  raw_json TEXT,
  captured_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (conversation_key) REFERENCES conversations(conversation_key)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
  ON messages (conversation_key, message_at);

CREATE INDEX IF NOT EXISTS idx_messages_captured_at
  ON messages (captured_at DESC);

CREATE TABLE IF NOT EXISTS capture_events (
  event_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'jd_dongdong',
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  conversation_key TEXT,
  message_dedupe_key TEXT,
  payload TEXT,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_capture_events_received_at
  ON capture_events (received_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  ip TEXT,
  user_agent TEXT,
  metadata TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
