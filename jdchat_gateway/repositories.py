from __future__ import annotations

import sqlite3
from typing import Any


def merge_sources(existing: str | None, incoming: str) -> str:
    parts = []
    for value in (existing or "").split(","):
        value = value.strip()
        if value and value not in parts:
            parts.append(value)
    if incoming not in parts:
        parts.append(incoming)
    return ",".join(parts)


def upsert_conversation(conn: sqlite3.Connection, conversation: dict[str, Any], message: dict[str, Any] | None) -> None:
    last_msg_id = message.get("msg_id") if message else None
    last_mid = message.get("mid") if message else None
    last_message_at = message.get("message_at") if message else None
    conn.execute(
        """
        INSERT INTO conversations (
          platform, conversation_key, vender_id, vender_name, seller_app, seller_pin_hash,
          customer_app, customer_pin_hash, customer_name, session_type, last_msg_id,
          last_mid, last_message_at, unread_count, last_read_mid, raw_customer
        )
        VALUES (
          :platform, :conversation_key, :vender_id, :vender_name, :seller_app, :seller_pin_hash,
          :customer_app, :customer_pin_hash, :customer_name, :session_type, :last_msg_id,
          :last_mid, :last_message_at, :unread_count, :last_read_mid, :raw_customer
        )
        ON CONFLICT(conversation_key) DO UPDATE SET
          vender_id = COALESCE(excluded.vender_id, conversations.vender_id),
          vender_name = COALESCE(excluded.vender_name, conversations.vender_name),
          seller_app = COALESCE(excluded.seller_app, conversations.seller_app),
          seller_pin_hash = COALESCE(excluded.seller_pin_hash, conversations.seller_pin_hash),
          customer_app = COALESCE(excluded.customer_app, conversations.customer_app),
          customer_pin_hash = COALESCE(excluded.customer_pin_hash, conversations.customer_pin_hash),
          customer_name = COALESCE(excluded.customer_name, conversations.customer_name),
          session_type = COALESCE(excluded.session_type, conversations.session_type),
          last_msg_id = COALESCE(excluded.last_msg_id, conversations.last_msg_id),
          last_mid = COALESCE(excluded.last_mid, conversations.last_mid),
          last_message_at = COALESCE(excluded.last_message_at, conversations.last_message_at),
          unread_count = COALESCE(excluded.unread_count, conversations.unread_count),
          last_read_mid = COALESCE(excluded.last_read_mid, conversations.last_read_mid),
          raw_customer = COALESCE(excluded.raw_customer, conversations.raw_customer),
          updated_at = CURRENT_TIMESTAMP
        """,
        {
            **conversation,
            "last_msg_id": last_msg_id,
            "last_mid": last_mid,
            "last_message_at": last_message_at,
        },
    )


def upsert_message(conn: sqlite3.Connection, message: dict[str, Any]) -> str:
    existing = conn.execute(
        "SELECT * FROM messages WHERE dedupe_key = ?",
        (message["dedupe_key"],),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO messages (
              dedupe_key, platform, conversation_key, msg_id, mid, local_id, direction,
              top_type, body_type, content, content_hash, media_url, media_width,
              media_height, media_size, template_type, template_payload, message_at,
              client_time, datetime_ms, timestamp_ms, read_flag, state, lang, from_app,
              from_pin_hash, from_client_type, from_art, to_app, to_pin_hash, source,
              raw_json, captured_at
            )
            VALUES (
              :dedupe_key, :platform, :conversation_key, :msg_id, :mid, :local_id, :direction,
              :top_type, :body_type, :content, :content_hash, :media_url, :media_width,
              :media_height, :media_size, :template_type, :template_payload, :message_at,
              :client_time, :datetime_ms, :timestamp_ms, :read_flag, :state, :lang, :from_app,
              :from_pin_hash, :from_client_type, :from_art, :to_app, :to_pin_hash, :source,
              :raw_json, :captured_at
            )
            """,
            message,
        )
        return "inserted"

    updates: dict[str, Any] = {
        "dedupe_key": message["dedupe_key"],
        "source": merge_sources(existing["source"], message["source"]),
    }
    changed = updates["source"] != existing["source"]

    nullable_fields = [
        "msg_id",
        "mid",
        "local_id",
        "direction",
        "top_type",
        "body_type",
        "content",
        "content_hash",
        "media_url",
        "media_width",
        "media_height",
        "media_size",
        "template_type",
        "template_payload",
        "message_at",
        "client_time",
        "datetime_ms",
        "timestamp_ms",
        "read_flag",
        "state",
        "lang",
        "from_app",
        "from_pin_hash",
        "from_client_type",
        "from_art",
        "to_app",
        "to_pin_hash",
        "raw_json",
    ]
    for field in nullable_fields:
        current = existing[field]
        incoming = message.get(field)
        if (current is None or current == "") and incoming not in (None, ""):
            updates[field] = incoming
            changed = True
        else:
            updates[field] = current

    if not changed:
        return "duplicate"

    conn.execute(
        """
        UPDATE messages SET
          msg_id = :msg_id,
          mid = :mid,
          local_id = :local_id,
          direction = :direction,
          top_type = :top_type,
          body_type = :body_type,
          content = :content,
          content_hash = :content_hash,
          media_url = :media_url,
          media_width = :media_width,
          media_height = :media_height,
          media_size = :media_size,
          template_type = :template_type,
          template_payload = :template_payload,
          message_at = :message_at,
          client_time = :client_time,
          datetime_ms = :datetime_ms,
          timestamp_ms = :timestamp_ms,
          read_flag = :read_flag,
          state = :state,
          lang = :lang,
          from_app = :from_app,
          from_pin_hash = :from_pin_hash,
          from_client_type = :from_client_type,
          from_art = :from_art,
          to_app = :to_app,
          to_pin_hash = :to_pin_hash,
          source = :source,
          raw_json = :raw_json,
          updated_at = CURRENT_TIMESTAMP
        WHERE dedupe_key = :dedupe_key
        """,
        updates,
    )
    return "updated"


def record_capture_event(conn: sqlite3.Connection, normalized: dict[str, Any]) -> None:
    message = normalized.get("message") or {}
    conn.execute(
        """
        INSERT OR IGNORE INTO capture_events (
          event_id, platform, source, event_type, conversation_key, message_dedupe_key,
          payload, captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized["event_id"],
            "jd_dongdong",
            normalized["source"],
            normalized["event_type"],
            normalized["conversation"]["conversation_key"],
            message.get("dedupe_key"),
            normalized["payload"],
            normalized["captured_at"],
        ),
    )


def list_conversations(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT conversation_key, vender_id, vender_name, customer_app, customer_pin_hash,
               customer_name, session_type, last_msg_id, last_mid, last_message_at,
               unread_count, updated_at
        FROM conversations
        ORDER BY COALESCE(last_message_at, updated_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_messages(conn: sqlite3.Connection, conversation_key: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT dedupe_key, msg_id, mid, direction, top_type, body_type, content,
               media_url, message_at, source, captured_at, updated_at
        FROM messages
        WHERE conversation_key = ?
        ORDER BY COALESCE(message_at, captured_at) DESC
        LIMIT ?
        """,
        (conversation_key, limit),
    ).fetchall()
    return [dict(row) for row in rows]
