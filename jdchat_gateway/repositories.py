from __future__ import annotations

import json
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
    existing = reuse_existing_message_identity(conn, message)
    if existing is None:
        conn.execute(
            """
            INSERT INTO messages (
              dedupe_key, platform, conversation_key, msg_id, mid, local_id, direction,
              top_type, body_type, content, content_hash, media_url, media_local_path,
              media_mime_type, media_storage_provider, media_download_status,
              media_download_error, media_width, media_height, media_size, template_type,
              template_payload, message_at, client_time, datetime_ms, timestamp_ms,
              read_flag, state, lang, from_app, from_pin_hash, from_client_type, from_art,
              to_app, to_pin_hash, source, raw_json, captured_at
            )
            VALUES (
              :dedupe_key, :platform, :conversation_key, :msg_id, :mid, :local_id, :direction,
              :top_type, :body_type, :content, :content_hash, :media_url, :media_local_path,
              :media_mime_type, :media_storage_provider, :media_download_status,
              :media_download_error, :media_width, :media_height, :media_size, :template_type,
              :template_payload, :message_at, :client_time, :datetime_ms, :timestamp_ms,
              :read_flag, :state, :lang, :from_app, :from_pin_hash, :from_client_type,
              :from_art, :to_app, :to_pin_hash, :source, :raw_json, :captured_at
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
        "media_local_path",
        "media_mime_type",
        "media_storage_provider",
        "media_download_status",
        "media_download_error",
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
          media_local_path = :media_local_path,
          media_mime_type = :media_mime_type,
          media_storage_provider = :media_storage_provider,
          media_download_status = :media_download_status,
          media_download_error = :media_download_error,
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


def reuse_existing_message_identity(conn: sqlite3.Connection, message: dict[str, Any]) -> sqlite3.Row | None:
    existing = find_existing_message(conn, message)
    if existing is not None and existing["dedupe_key"] != message["dedupe_key"]:
        message["dedupe_key"] = existing["dedupe_key"]
    return existing


def find_existing_message(conn: sqlite3.Connection, message: dict[str, Any]) -> sqlite3.Row | None:
    existing = conn.execute(
        "SELECT * FROM messages WHERE dedupe_key = ?",
        (message["dedupe_key"],),
    ).fetchone()
    if existing is not None or message.get("source") != "dom":
        return existing

    return conn.execute(
        """
        SELECT *
        FROM messages
        WHERE conversation_key = ?
          AND instr(',' || source || ',', ',dom,') > 0
          AND direction = ?
          AND body_type = ?
          AND COALESCE(content, '') = COALESCE(?, '')
          AND COALESCE(media_url, '') = COALESCE(?, '')
          AND COALESCE(json_extract(raw_json, '$.displayTime'), '') =
              COALESCE(json_extract(?, '$.displayTime'), '')
        ORDER BY id
        LIMIT 1
        """,
        (
            message.get("conversation_key"),
            message.get("direction"),
            message.get("body_type"),
            message.get("content"),
            message.get("media_url"),
            message.get("raw_json") or "{}",
        ),
    ).fetchone()


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
            normalized["conversation"]["platform"],
            normalized["source"],
            normalized["event_type"],
            normalized["conversation"]["conversation_key"],
            message.get("dedupe_key"),
            normalized["payload"],
            normalized["captured_at"],
        ),
    )


def list_conversations(
    conn: sqlite3.Connection,
    limit: int,
    *,
    offset: int = 0,
    q: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    where_sql, params = conversation_filters(q=q, source=source)
    rows = conn.execute(
        f"""
        SELECT c.conversation_key, c.vender_id, c.vender_name, c.customer_app, c.customer_pin_hash,
               c.customer_name, c.session_type, c.last_msg_id, c.last_mid, c.last_message_at,
               c.unread_count, c.updated_at, COUNT(m.id) AS message_count,
               MAX(m.captured_at) AS last_captured_at, GROUP_CONCAT(DISTINCT m.source) AS sources
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_key = c.conversation_key
        {where_sql}
        GROUP BY c.conversation_key
        ORDER BY COALESCE(c.last_message_at, MAX(m.captured_at), c.updated_at) DESC
        LIMIT ?
        OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def count_conversations(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    source: str | None = None,
) -> int:
    where_sql, params = conversation_filters(q=q, source=source)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM conversations c
        {where_sql}
        """,
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def conversation_filters(*, q: str | None = None, source: str | None = None) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            """
            (
              c.conversation_key LIKE ?
              OR c.vender_id LIKE ?
              OR c.vender_name LIKE ?
              OR c.customer_app LIKE ?
              OR c.customer_pin_hash LIKE ?
              OR c.customer_name LIKE ?
              OR c.session_type LIKE ?
              OR c.last_msg_id LIKE ?
            )
            """
        )
        params.extend([like] * 8)

    if source:
        conditions.append(
            """
            EXISTS (
              SELECT 1
              FROM messages source_messages
              WHERE source_messages.conversation_key = c.conversation_key
                AND instr(',' || source_messages.source || ',', ',' || ? || ',') > 0
            )
            """
        )
        params.append(source)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_sql, params


def list_messages(
    conn: sqlite3.Connection,
    conversation_key: str,
    limit: int,
    *,
    order: str = "desc",
    before: str | None = None,
) -> list[dict[str, Any]]:
    order_sql = "ASC" if order == "asc" else "DESC"
    id_order_sql = "ASC" if order == "asc" else "DESC"
    conditions = ["conversation_key = ?"]
    params: list[Any] = [conversation_key]
    if before:
        conditions.append("COALESCE(message_at, captured_at) < ?")
        params.append(before)

    rows = conn.execute(
        f"""
        SELECT dedupe_key, msg_id, mid, local_id, direction, top_type, body_type, content,
               media_url, media_local_path, media_mime_type, media_storage_provider,
               media_download_status, media_download_error, media_width, media_height,
               media_size, template_type, template_payload, message_at, client_time,
               datetime_ms, timestamp_ms, read_flag, state, lang, from_app, to_app,
               source, captured_at, updated_at
        FROM messages
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(message_at, captured_at) {order_sql}, id {id_order_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def capture_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM conversations) AS conversations,
          (SELECT COUNT(*) FROM messages) AS messages,
          (SELECT COUNT(*) FROM capture_events) AS capture_events,
          (SELECT COUNT(*) FROM audit_logs) AS audit_logs
        """
    ).fetchone()
    latest_message = conn.execute(
        """
        SELECT conversation_key, direction, body_type, source, message_at, captured_at
        FROM messages
        ORDER BY COALESCE(message_at, captured_at) DESC
        LIMIT 1
        """
    ).fetchone()
    latest_event = conn.execute(
        """
        SELECT source, event_type, conversation_key, captured_at, received_at
        FROM capture_events
        ORDER BY received_at DESC
        LIMIT 1
        """
    ).fetchone()
    event_sources = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM capture_events
        GROUP BY source
        ORDER BY count DESC, source
        """
    ).fetchall()
    message_types = conn.execute(
        """
        SELECT direction, body_type, COUNT(*) AS count
        FROM messages
        GROUP BY direction, body_type
        ORDER BY count DESC, direction, body_type
        """
    ).fetchall()
    return {
        "totals": dict(totals) if totals else {},
        "latest_message": dict(latest_message) if latest_message else None,
        "latest_event": dict(latest_event) if latest_event else None,
        "event_sources": [dict(row) for row in event_sources],
        "message_types": [dict(row) for row in message_types],
    }


def list_capture_events_recent(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id, source, event_type, conversation_key, message_dedupe_key,
               payload, captured_at, received_at
        FROM capture_events
        ORDER BY received_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [capture_event_metadata(dict(row)) for row in rows]


def capture_event_metadata(row: dict[str, Any]) -> dict[str, Any]:
    page_context = extract_page_context(row.pop("payload", None))
    return {
        **row,
        "active_sidebar_tab": page_context.get("activeSidebarTab"),
        "active_sidebar_tab_label": page_context.get("activeSidebarTabLabel"),
        "history_list_visible": page_context.get("historyListVisible"),
        "history_item_count": page_context.get("historyItemCount"),
        "message_node_count": page_context.get("messageNodeCount"),
    }


def extract_page_context(payload_json: str | None) -> dict[str, Any]:
    if not payload_json:
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict) and isinstance(nested_payload.get("pageContext"), dict):
        return nested_payload["pageContext"]
    if isinstance(payload.get("pageContext"), dict):
        return payload["pageContext"]
    return {}
