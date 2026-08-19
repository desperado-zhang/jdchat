from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from jdchat_gateway.dedupe import content_hash, hash_identifier, sha256_text

RECEPTION_PLATFORM = "jd_jingmai_reception"
ReceptionSource = Literal["reception_chatlog", "reception_dom"]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
LOCAL_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)
SENSITIVE_KEY_FRAGMENTS = (
    "cookie",
    "token",
    "access_token",
    "authorization",
    "password",
    "secret",
    "sign",
    "aid",
    "pin",
    "customer",
    "waiter",
    "service",
)
SENSITIVE_KEY_EXACT = {"cid", "sid", "uuid", "appid", "mallid", "tocid"}


class ReceptionChatLogEventIn(BaseModel):
    event_id: str | None = Field(default=None, alias="eventId")
    source: ReceptionSource
    event_type: str = Field(default="message", alias="eventType")
    conversation: dict[str, Any] | None = None
    message: dict[str, Any] | None = None
    payload: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    captured_at: str | None = Field(default=None, alias="capturedAt")

    model_config = {"populate_by_name": True, "extra": "allow"}


class ReceptionChatLogBatchIn(BaseModel):
    plugin_instance_id: str | None = Field(default=None, alias="pluginInstanceId")
    waiter_account_hash: str | None = Field(default=None, alias="waiterAccountHash")
    shop_id: str | None = Field(default=None, alias="shopId")
    events: list[ReceptionChatLogEventIn]

    model_config = {"populate_by_name": True, "extra": "allow"}


class ReceptionCaptureRejected(BaseModel):
    event_id: str | None = Field(default=None, alias="eventId")
    reason: str

    model_config = {"populate_by_name": True}


class ReceptionCaptureResponse(BaseModel):
    accepted: int
    inserted: int
    updated: int
    duplicates: int
    rejected: list[ReceptionCaptureRejected]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def pick(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compact_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    normalized = key_lower.replace("_", "").replace("-", "")
    return any(part in key_lower for part in SENSITIVE_KEY_FRAGMENTS) or normalized in SENSITIVE_KEY_EXACT


def redacted_value(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else compact_json(value)
    text = "" if text is None else str(text)
    return {"redacted": True, "len": len(text), "hash": sha256_text(text)}


def redact_sensitive(value: Any, key: str = "") -> Any:
    sensitive = is_sensitive_key(key) if key else False
    if isinstance(value, dict):
        return {k: redact_sensitive(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item, key) for item in value]
    if sensitive and value is not None:
        return redacted_value(value)
    return value


def parse_reception_time(value: Any) -> str | None:
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
        return parsed.astimezone(UTC).isoformat()
    except ValueError:
        pass

    for fmt in LOCAL_DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=SHANGHAI_TZ).astimezone(UTC).isoformat()
        except ValueError:
            continue
    return None


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def event_message(event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message")
    if isinstance(message, dict):
        return message
    payload = event.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("message") or payload.get("chatLogMessage")
        if isinstance(nested, dict):
            return nested
    return None


def normalize_reception_chatlog_event(
    event: dict[str, Any],
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = event_message(event)
    session = normalize_reception_session(event=event, message=message, batch=batch)
    normalized_message = None
    if message:
        normalized_message = normalize_reception_message(event=event, message=message, session=session)

    event_id = pick(event, "event_id", "eventId")
    if not event_id:
        stable_basis = compact_json(
            {
                "source": event.get("source"),
                "event_type": event.get("event_type") or event.get("eventType"),
                "conversation_key": session["conversation_key"],
                "message": (
                    normalized_message["dedupe_key"]
                    if normalized_message
                    else compact_json(event.get("payload"))
                ),
                "captured_at": pick(event, "captured_at", "capturedAt"),
            }
        )
        event_id = sha256_text(stable_basis)

    return {
        "event_id": event_id,
        "source": event["source"],
        "event_type": pick(event, "event_type", "eventType") or "message",
        "session": session,
        "message": normalized_message,
        "payload": compact_json(redact_sensitive(event)),
        "captured_at": pick(event, "captured_at", "capturedAt") or now_iso(),
    }


def normalize_reception_session(
    *,
    event: dict[str, Any],
    message: dict[str, Any] | None,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conversation = dict(event.get("conversation") or {})
    message = message or {}
    batch = batch or {}

    raw_cid = pick(conversation, "cid") or pick(event, "cid") or pick(message, "cid")
    cid_hash = (
        pick(conversation, "cid_hash", "cidHash", "conversationCidHash")
        or pick(event, "cid_hash", "cidHash")
        or (hash_identifier(str(raw_cid)) if raw_cid else None)
    )
    customer_hash = (
        pick(conversation, "customer_hash", "customerHash", "customer_pin_hash", "customerPinHash")
        or hash_identifier(pick(conversation, "customer") or pick(message, "customer"))
    )
    waiter_hash = (
        pick(
            conversation,
            "waiter_hash",
            "waiterHash",
            "service_hash",
            "serviceHash",
            "seller_pin_hash",
            "sellerPinHash",
        )
        or pick(batch, "waiterAccountHash")
        or hash_identifier(pick(conversation, "service", "waiter") or pick(message, "waiter"))
    )
    mall_id = pick(conversation, "mall_id", "mallId", "vender_id", "venderId") or pick(message, "mallId", "mall_id")
    mall_name = pick(conversation, "mall_name", "mallName", "vender_name", "venderName")
    session_type = pick(conversation, "session_type", "sessionType")
    session_type_desc = pick(conversation, "session_type_desc", "sessionTypeDesc")

    conversation_key = pick(conversation, "conversation_key", "conversationKey")
    if not conversation_key:
        identity = cid_hash or f"{mall_id or ''}:{customer_hash or ''}:{waiter_hash or ''}:{session_type or ''}"
        conversation_key = sha256_text(f"{RECEPTION_PLATFORM}:{identity}")

    raw_session = dict(conversation)
    if cid_hash and "cidHash" not in raw_session and "cid_hash" not in raw_session:
        raw_session["cidHash"] = cid_hash

    return {
        "platform": RECEPTION_PLATFORM,
        "conversation_key": conversation_key,
        "cid_hash": cid_hash,
        "mall_id": mall_id,
        "mall_name": mall_name,
        "group_id": pick(conversation, "group_id", "groupId"),
        "group_name": pick(conversation, "group_name", "groupName"),
        "customer_hash": customer_hash,
        "waiter_hash": waiter_hash,
        "session_type": str(session_type) if session_type is not None else None,
        "session_type_desc": str(session_type_desc) if session_type_desc is not None else None,
        "consultation_at": parse_reception_time(pick(conversation, "consultationDate", "consultation_at")),
        "allocate_at": parse_reception_time(pick(conversation, "allocateTime", "allocate_at")),
        "goods_id": pick(conversation, "goods_id", "goodsId", "pid"),
        "goods_name": pick(conversation, "goods_name", "goodsName"),
        "raw_session": compact_json(redact_sensitive(raw_session)),
        "captured_at": pick(event, "captured_at", "capturedAt") or now_iso(),
    }


def normalize_reception_message(
    *,
    event: dict[str, Any],
    message: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    cid_hash = session.get("cid_hash")
    raw_mid = pick(message, "mid")
    uuid = pick(message, "uuid")
    sid = pick(message, "sid")
    explicit_msg_id = pick(message, "id", "msgId", "msg_id")

    if explicit_msg_id:
        msg_id = str(explicit_msg_id)
    elif raw_mid not in (None, "") and cid_hash:
        msg_id = f"jm:{cid_hash}:{raw_mid}"
    elif uuid and cid_hash:
        msg_id = f"jm:{cid_hash}:{hash_identifier(str(uuid))}"
    elif uuid:
        msg_id = f"jm:uuid:{hash_identifier(str(uuid))}"
    else:
        msg_id = None

    media_url = pick(message, "imgUrl", "img_url", "mediaUrl", "media_url")
    body_type = "image" if media_url else str(pick(message, "bodyType", "body_type", "type") or "text")
    content = pick(message, "content", "msg", "text")
    if content is not None:
        content = str(content)

    waiter_send = boolish(pick(message, "waiterSend", "waiter_send"))
    direction = "seller_or_waiter" if waiter_send else "customer_or_external"
    message_at = parse_reception_time(pick(message, "created", "messageAt", "message_at", "time"))
    customer_hash = (
        pick(event.get("conversation") or {}, "customer_hash", "customerHash", "customer_pin_hash", "customerPinHash")
        or hash_identifier(pick(message, "customer"))
        or session.get("customer_hash")
    )
    waiter_hash = (
        pick(event.get("conversation") or {}, "waiter_hash", "waiterHash", "service_hash", "serviceHash")
        or hash_identifier(pick(message, "waiter"))
        or session.get("waiter_hash")
    )
    from_hash = waiter_hash if waiter_send else customer_hash
    to_hash = customer_hash if waiter_send else waiter_hash
    msg_content_hash = content_hash(content)
    local_id = pick(message, "sidHash", "localId", "local_id") or (hash_identifier(str(sid)) if sid else None)
    dedupe_key = reception_dedupe_key(
        conversation_key=session["conversation_key"],
        cid_hash=cid_hash,
        msg_id=msg_id,
        mid=raw_mid,
        uuid=uuid,
        timestamp=message_at or pick(message, "created", "messageAt", "message_at", "time"),
        local_id=local_id,
        direction=direction,
        body_type=body_type,
        content_hash_value=msg_content_hash,
        media_url=media_url,
    )

    return {
        "dedupe_key": dedupe_key,
        "conversation_key": session["conversation_key"],
        "msg_id": msg_id,
        "mid": str(raw_mid) if raw_mid not in (None, "") else None,
        "local_id": local_id,
        "direction": direction,
        "body_type": body_type,
        "content": content,
        "content_hash": msg_content_hash,
        "media_url": media_url,
        "media_width": to_int(pick(message, "width")),
        "media_height": to_int(pick(message, "height")),
        "message_at": message_at,
        "lang": pick(message, "lang"),
        "from_hash": from_hash,
        "to_hash": to_hash,
        "source": event["source"],
        "raw_json": compact_json(redact_sensitive(message)),
        "captured_at": pick(event, "captured_at", "capturedAt") or now_iso(),
    }


def reception_dedupe_key(
    *,
    conversation_key: str,
    cid_hash: str | None,
    msg_id: str | None = None,
    mid: Any = None,
    uuid: Any = None,
    timestamp: str | None = None,
    local_id: str | None = None,
    direction: str | None = None,
    body_type: str | None = None,
    content_hash_value: str | None = None,
    media_url: str | None = None,
) -> str:
    if msg_id:
        source = f"msg_id:{RECEPTION_PLATFORM}:{msg_id}"
    elif cid_hash and mid not in (None, ""):
        source = f"mid:{RECEPTION_PLATFORM}:{cid_hash}:{mid}"
    elif cid_hash and uuid:
        source = f"uuid:{RECEPTION_PLATFORM}:{cid_hash}:{hash_identifier(str(uuid))}"
    else:
        source = (
            "fallback:"
            f"{conversation_key}:"
            f"{local_id or ''}:"
            f"{timestamp or ''}:"
            f"{direction or 'unknown'}:"
            f"{body_type or ''}:"
            f"{content_hash_value or ''}:"
            f"{media_url or ''}"
        )
    return sha256_text(source)


def upsert_reception_chatlog_session(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    message: dict[str, Any] | None,
) -> None:
    last_msg_id = message.get("msg_id") if message else None
    last_mid = message.get("mid") if message else None
    last_message_at = message.get("message_at") if message else None
    conn.execute(
        """
        INSERT INTO reception_chatlog_sessions (
          platform, conversation_key, cid_hash, mall_id, mall_name, group_id, group_name,
          customer_hash, waiter_hash, session_type, session_type_desc, consultation_at,
          allocate_at, goods_id, goods_name, last_msg_id, last_mid, last_message_at,
          raw_session, captured_at
        )
        VALUES (
          :platform, :conversation_key, :cid_hash, :mall_id, :mall_name, :group_id, :group_name,
          :customer_hash, :waiter_hash, :session_type, :session_type_desc, :consultation_at,
          :allocate_at, :goods_id, :goods_name, :last_msg_id, :last_mid, :last_message_at,
          :raw_session, :captured_at
        )
        ON CONFLICT(conversation_key) DO UPDATE SET
          cid_hash = COALESCE(excluded.cid_hash, reception_chatlog_sessions.cid_hash),
          mall_id = COALESCE(excluded.mall_id, reception_chatlog_sessions.mall_id),
          mall_name = COALESCE(excluded.mall_name, reception_chatlog_sessions.mall_name),
          group_id = COALESCE(excluded.group_id, reception_chatlog_sessions.group_id),
          group_name = COALESCE(excluded.group_name, reception_chatlog_sessions.group_name),
          customer_hash = COALESCE(excluded.customer_hash, reception_chatlog_sessions.customer_hash),
          waiter_hash = COALESCE(excluded.waiter_hash, reception_chatlog_sessions.waiter_hash),
          session_type = COALESCE(excluded.session_type, reception_chatlog_sessions.session_type),
          session_type_desc = COALESCE(excluded.session_type_desc, reception_chatlog_sessions.session_type_desc),
          consultation_at = COALESCE(excluded.consultation_at, reception_chatlog_sessions.consultation_at),
          allocate_at = COALESCE(excluded.allocate_at, reception_chatlog_sessions.allocate_at),
          goods_id = COALESCE(excluded.goods_id, reception_chatlog_sessions.goods_id),
          goods_name = COALESCE(excluded.goods_name, reception_chatlog_sessions.goods_name),
          last_msg_id = COALESCE(excluded.last_msg_id, reception_chatlog_sessions.last_msg_id),
          last_mid = COALESCE(excluded.last_mid, reception_chatlog_sessions.last_mid),
          last_message_at = COALESCE(excluded.last_message_at, reception_chatlog_sessions.last_message_at),
          raw_session = COALESCE(excluded.raw_session, reception_chatlog_sessions.raw_session),
          captured_at = COALESCE(excluded.captured_at, reception_chatlog_sessions.captured_at),
          updated_at = CURRENT_TIMESTAMP
        """,
        {
            **session,
            "last_msg_id": last_msg_id,
            "last_mid": last_mid,
            "last_message_at": last_message_at,
        },
    )


def upsert_reception_chatlog_message(conn: sqlite3.Connection, message: dict[str, Any]) -> str:
    existing = conn.execute(
        "SELECT dedupe_key FROM reception_chatlog_messages WHERE dedupe_key = ?",
        (message["dedupe_key"],),
    ).fetchone()
    if existing is not None:
        return "duplicate"

    conn.execute(
        """
        INSERT INTO reception_chatlog_messages (
          dedupe_key, conversation_key, msg_id, mid, local_id, direction, body_type, content,
          content_hash, media_url, media_width, media_height, message_at, lang, from_hash,
          to_hash, source, raw_json, captured_at
        )
        VALUES (
          :dedupe_key, :conversation_key, :msg_id, :mid, :local_id, :direction, :body_type, :content,
          :content_hash, :media_url, :media_width, :media_height, :message_at, :lang, :from_hash,
          :to_hash, :source, :raw_json, :captured_at
        )
        """,
        message,
    )
    return "inserted"


def record_reception_chatlog_event(conn: sqlite3.Connection, normalized: dict[str, Any]) -> None:
    message = normalized.get("message") or {}
    session = normalized["session"]
    conn.execute(
        """
        INSERT OR IGNORE INTO reception_chatlog_events (
          event_id, source, event_type, conversation_key, message_dedupe_key, payload, captured_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized["event_id"],
            normalized["source"],
            normalized["event_type"],
            session["conversation_key"],
            message.get("dedupe_key"),
            normalized["payload"],
            normalized["captured_at"],
        ),
    )


def list_reception_chatlog_sessions(
    conn: sqlite3.Connection,
    limit: int,
    *,
    q: str | None = None,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            """
            (
              conversation_key LIKE ?
              OR cid_hash LIKE ?
              OR mall_id LIKE ?
              OR mall_name LIKE ?
              OR customer_hash LIKE ?
              OR waiter_hash LIKE ?
              OR goods_id LIKE ?
              OR goods_name LIKE ?
            )
            """
        )
        params.extend([like] * 8)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"""
        SELECT s.conversation_key, s.cid_hash, s.mall_id, s.mall_name, s.group_id, s.group_name,
               s.customer_hash, s.waiter_hash, s.session_type, s.session_type_desc,
               s.goods_id, s.goods_name, s.last_msg_id, s.last_mid, s.last_message_at,
               s.updated_at, COUNT(m.id) AS message_count, MAX(m.captured_at) AS last_captured_at
        FROM reception_chatlog_sessions s
        LEFT JOIN reception_chatlog_messages m ON m.conversation_key = s.conversation_key
        {where_sql}
        GROUP BY s.conversation_key
        ORDER BY COALESCE(s.last_message_at, MAX(m.captured_at), s.updated_at) DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_reception_chatlog_messages(
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
        SELECT dedupe_key, msg_id, mid, local_id, direction, body_type, content, content_hash,
               media_url, media_width, media_height, message_at, lang, source, captured_at, updated_at
        FROM reception_chatlog_messages
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(message_at, captured_at) {order_sql}, id {id_order_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def reception_chatlog_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    totals = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM reception_chatlog_sessions) AS sessions,
          (SELECT COUNT(*) FROM reception_chatlog_messages) AS messages,
          (SELECT COUNT(*) FROM reception_chatlog_events) AS events
        """
    ).fetchone()
    latest_message = conn.execute(
        """
        SELECT conversation_key, direction, body_type, message_at, captured_at
        FROM reception_chatlog_messages
        ORDER BY COALESCE(message_at, captured_at) DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "totals": dict(totals) if totals else {},
        "latest_message": dict(latest_message) if latest_message else None,
    }
