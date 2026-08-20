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


class ReceptionCaptureJobProgressIn(BaseModel):
    job_key: str | None = Field(default=None, alias="jobKey")
    capture_date: str | None = Field(default=None, alias="captureDate")
    mode: str = "unknown"
    capture_status: str = Field(default="running", alias="status")
    total_count: int | None = Field(default=None, alias="totalCount")
    total_pages: int | None = Field(default=None, alias="totalPages")
    current_page: int | None = Field(default=None, alias="currentPage")
    opened_rows: int | None = Field(default=None, alias="openedRows")
    captured_details: int | None = Field(default=None, alias="capturedDetails")
    stable_rounds: int | None = Field(default=None, alias="stableRounds")
    failure_count: int | None = Field(default=None, alias="failureCount")
    last_error: str | None = Field(default=None, alias="lastError")
    last_action: str | None = Field(default=None, alias="lastAction")
    status_payload: dict[str, Any] | None = Field(default=None, alias="statusPayload")
    started_at: str | None = Field(default=None, alias="startedAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")

    model_config = {"populate_by_name": True, "extra": "allow"}


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


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
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


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def add_tag(tags: list[str], value: Any) -> None:
    if value in (None, "", False):
        return
    if isinstance(value, list | tuple | set):
        for item in value:
            add_tag(tags, item)
        return
    text = str(value).strip()
    if not text:
        return
    for part in text.replace(",", "、").replace("|", "、").split("、"):
        part = part.strip()
        if part and part not in tags:
            tags.append(part)


def normalize_result_tags(conversation: dict[str, Any]) -> str | None:
    tags: list[str] = []
    add_tag(tags, pick(conversation, "result_tags", "resultTags", "resultLabel", "resultLabels"))

    if boolish(pick(conversation, "reply30s", "reply_30s")):
        add_tag(tags, "30秒内未回复")

    promote_order = pick(conversation, "promoteOrder", "promote_order", "orderTag", "orderStatus")
    if promote_order not in (None, ""):
        promote_text = str(promote_order)
        if "下单" in promote_text:
            add_tag(tags, promote_text)
        else:
            add_tag(tags, "已下单" if boolish(promote_order) else "未下单")

    if boolish(pick(conversation, "repeatIn24h", "repeat_in_24h")):
        add_tag(tags, "24小时重复进线")
    if boolish(pick(conversation, "transferStatus", "transfer_status")):
        add_tag(tags, "24h转平台")

    add_tag(tags, pick(conversation, "evaluation"))
    add_tag(tags, pick(conversation, "solveOption", "solve_option"))
    return "、".join(tags) if tags else None


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
    consultation_type = pick(conversation, "consultation_type", "consultationType") or session_type_desc
    customer_identity = (
        pick(conversation, "customer", "customerName", "customerDisplayId", "customer_display_id", "customerPin")
        or pick(message, "customer", "customerDisplayId", "customer_display_id")
    )
    waiter_identity = (
        pick(conversation, "service", "waiter", "waiterName", "waiterDisplayId", "waiter_display_id")
        or pick(message, "waiter", "waiterDisplayId", "waiter_display_id")
    )
    customer_display_id = text_or_none(
        pick(
            conversation,
            "customer_display_id",
            "customerDisplayId",
            "customer",
            "customerName",
            "customerPin",
            "buyerNick",
        )
        or pick(message, "customer_display_id", "customerDisplayId", "customer")
    )
    waiter_display_id = text_or_none(
        pick(
            conversation,
            "waiter_display_id",
            "waiterDisplayId",
            "service",
            "waiter",
            "waiterName",
            "serviceName",
        )
        or pick(message, "waiter_display_id", "waiterDisplayId", "waiter")
    )
    transfer_waiter_display_id = text_or_none(
        pick(
            conversation,
            "transfer_waiter_display_id",
            "transferWaiterDisplayId",
            "transferWaiter",
            "transfer_waiter",
        )
    )
    customer_hash = customer_hash or hash_identifier(customer_identity)
    waiter_hash = waiter_hash or hash_identifier(waiter_identity)

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
        "customer_display_id": customer_display_id,
        "waiter_display_id": waiter_display_id,
        "transfer_waiter_display_id": transfer_waiter_display_id,
        "result_tags": normalize_result_tags(conversation),
        "session_type": str(session_type) if session_type is not None else None,
        "session_type_desc": str(session_type_desc) if session_type_desc is not None else None,
        "consultation_type": str(consultation_type) if consultation_type is not None else None,
        "consultation_at": parse_reception_time(pick(conversation, "consultationDate", "consultation_at")),
        "allocate_at": parse_reception_time(pick(conversation, "allocateTime", "allocate_at")),
        "goods_id": pick(conversation, "goods_id", "goodsId", "pid"),
        "goods_name": pick(conversation, "goods_name", "goodsName"),
        "new_response_avg_seconds": to_float(
            pick(conversation, "new_response_avg_seconds", "newResponseAvgSeconds", "newResponseAvgSpeed")
        ),
        "first_response_at": parse_reception_time(
            pick(conversation, "firstResponseAt", "first_response_at", "responseTime")
        ),
        "session_duration_minutes": to_float(
            pick(conversation, "sessionDurationMinutes", "session_duration_minutes", "sessionDuration")
        ),
        "evaluation_source": text_or_none(pick(conversation, "evaluationSource", "evaluation_source")),
        "evaluation_at": parse_reception_time(pick(conversation, "evaluationTime", "evaluation_at")),
        "dissatisfied_reason": text_or_none(
            pick(conversation, "dissatisfiedReason", "dissatisfied_reason", "unsatisfiedReason")
        ),
        "intent_primary": text_or_none(
            pick(conversation, "intentPrimary", "intent_primary", "intent1", "intentionOne")
        ),
        "intent_secondary": text_or_none(
            pick(conversation, "intentSecondary", "intent_secondary", "intent2", "intentionTwo")
        ),
        "scene": text_or_none(pick(conversation, "scene")),
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
        or hash_identifier(pick(message, "customer", "customerDisplayId", "customer_display_id"))
        or session.get("customer_hash")
    )
    waiter_hash = (
        pick(event.get("conversation") or {}, "waiter_hash", "waiterHash", "service_hash", "serviceHash")
        or hash_identifier(pick(message, "waiter", "waiterDisplayId", "waiter_display_id"))
        or session.get("waiter_hash")
    )
    from_hash = waiter_hash if waiter_send else customer_hash
    to_hash = customer_hash if waiter_send else waiter_hash
    customer_display_id = text_or_none(
        pick(message, "customer_display_id", "customerDisplayId", "customer")
        or pick(event.get("conversation") or {}, "customer_display_id", "customerDisplayId", "customer")
        or session.get("customer_display_id")
    )
    waiter_display_id = text_or_none(
        pick(message, "waiter_display_id", "waiterDisplayId", "waiter")
        or pick(event.get("conversation") or {}, "waiter_display_id", "waiterDisplayId", "service", "waiter")
        or session.get("waiter_display_id")
    )
    from_display_id = waiter_display_id if waiter_send else customer_display_id
    to_display_id = customer_display_id if waiter_send else waiter_display_id
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
        "from_display_id": from_display_id,
        "to_display_id": to_display_id,
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
          customer_hash, waiter_hash, customer_display_id, waiter_display_id,
          transfer_waiter_display_id, result_tags, session_type, session_type_desc,
          consultation_type, consultation_at, allocate_at, goods_id, goods_name,
          new_response_avg_seconds, first_response_at, session_duration_minutes,
          evaluation_source, evaluation_at, dissatisfied_reason, intent_primary,
          intent_secondary, scene, last_msg_id, last_mid, last_message_at, raw_session,
          captured_at
        )
        VALUES (
          :platform, :conversation_key, :cid_hash, :mall_id, :mall_name, :group_id, :group_name,
          :customer_hash, :waiter_hash, :customer_display_id, :waiter_display_id,
          :transfer_waiter_display_id, :result_tags, :session_type, :session_type_desc,
          :consultation_type, :consultation_at, :allocate_at, :goods_id, :goods_name,
          :new_response_avg_seconds, :first_response_at, :session_duration_minutes,
          :evaluation_source, :evaluation_at, :dissatisfied_reason, :intent_primary,
          :intent_secondary, :scene, :last_msg_id, :last_mid, :last_message_at,
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
          customer_display_id = COALESCE(excluded.customer_display_id, reception_chatlog_sessions.customer_display_id),
          waiter_display_id = COALESCE(excluded.waiter_display_id, reception_chatlog_sessions.waiter_display_id),
          transfer_waiter_display_id = COALESCE(
            excluded.transfer_waiter_display_id,
            reception_chatlog_sessions.transfer_waiter_display_id
          ),
          result_tags = COALESCE(excluded.result_tags, reception_chatlog_sessions.result_tags),
          session_type = COALESCE(excluded.session_type, reception_chatlog_sessions.session_type),
          session_type_desc = COALESCE(excluded.session_type_desc, reception_chatlog_sessions.session_type_desc),
          consultation_type = COALESCE(excluded.consultation_type, reception_chatlog_sessions.consultation_type),
          consultation_at = COALESCE(excluded.consultation_at, reception_chatlog_sessions.consultation_at),
          allocate_at = COALESCE(excluded.allocate_at, reception_chatlog_sessions.allocate_at),
          goods_id = COALESCE(excluded.goods_id, reception_chatlog_sessions.goods_id),
          goods_name = COALESCE(excluded.goods_name, reception_chatlog_sessions.goods_name),
          new_response_avg_seconds = COALESCE(
            excluded.new_response_avg_seconds,
            reception_chatlog_sessions.new_response_avg_seconds
          ),
          first_response_at = COALESCE(excluded.first_response_at, reception_chatlog_sessions.first_response_at),
          session_duration_minutes = COALESCE(
            excluded.session_duration_minutes,
            reception_chatlog_sessions.session_duration_minutes
          ),
          evaluation_source = COALESCE(excluded.evaluation_source, reception_chatlog_sessions.evaluation_source),
          evaluation_at = COALESCE(excluded.evaluation_at, reception_chatlog_sessions.evaluation_at),
          dissatisfied_reason = COALESCE(excluded.dissatisfied_reason, reception_chatlog_sessions.dissatisfied_reason),
          intent_primary = COALESCE(excluded.intent_primary, reception_chatlog_sessions.intent_primary),
          intent_secondary = COALESCE(excluded.intent_secondary, reception_chatlog_sessions.intent_secondary),
          scene = COALESCE(excluded.scene, reception_chatlog_sessions.scene),
          last_msg_id = CASE
            WHEN excluded.last_message_at IS NULL THEN reception_chatlog_sessions.last_msg_id
            WHEN reception_chatlog_sessions.last_message_at IS NULL THEN excluded.last_msg_id
            WHEN excluded.last_message_at >= reception_chatlog_sessions.last_message_at THEN excluded.last_msg_id
            ELSE reception_chatlog_sessions.last_msg_id
          END,
          last_mid = CASE
            WHEN excluded.last_message_at IS NULL THEN reception_chatlog_sessions.last_mid
            WHEN reception_chatlog_sessions.last_message_at IS NULL THEN excluded.last_mid
            WHEN excluded.last_message_at >= reception_chatlog_sessions.last_message_at THEN excluded.last_mid
            ELSE reception_chatlog_sessions.last_mid
          END,
          last_message_at = CASE
            WHEN excluded.last_message_at IS NULL THEN reception_chatlog_sessions.last_message_at
            WHEN reception_chatlog_sessions.last_message_at IS NULL THEN excluded.last_message_at
            WHEN excluded.last_message_at >= reception_chatlog_sessions.last_message_at THEN excluded.last_message_at
            ELSE reception_chatlog_sessions.last_message_at
          END,
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
          to_hash, from_display_id, to_display_id, source, raw_json, captured_at
        )
        VALUES (
          :dedupe_key, :conversation_key, :msg_id, :mid, :local_id, :direction, :body_type, :content,
          :content_hash, :media_url, :media_width, :media_height, :message_at, :lang, :from_hash,
          :to_hash, :from_display_id, :to_display_id, :source, :raw_json, :captured_at
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


def upsert_reception_capture_job(
    conn: sqlite3.Connection,
    progress: ReceptionCaptureJobProgressIn,
) -> dict[str, Any]:
    capture_date = progress.capture_date or datetime.now(SHANGHAI_TZ).date().isoformat()
    job_key = progress.job_key or f"reception_chatlog:{capture_date}"
    now = now_iso()
    started_at = progress.started_at or now
    finished_at = progress.finished_at
    if not finished_at and progress.capture_status in {"finished", "failed", "stopped"}:
        finished_at = now

    values = {
        "job_key": job_key,
        "capture_date": capture_date,
        "mode": progress.mode,
        "status": progress.capture_status,
        "total_count": progress.total_count,
        "total_pages": progress.total_pages,
        "current_page": progress.current_page,
        "opened_rows": progress.opened_rows,
        "captured_details": progress.captured_details,
        "stable_rounds": progress.stable_rounds,
        "failure_count": progress.failure_count,
        "last_error": progress.last_error,
        "last_action": progress.last_action,
        "status_payload": compact_json(progress.status_payload),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    conn.execute(
        """
        INSERT INTO reception_chatlog_capture_jobs (
          job_key, capture_date, mode, status, total_count, total_pages,
          current_page, opened_rows, captured_details, stable_rounds, failure_count,
          last_error, last_action, status_payload, started_at, finished_at
        )
        VALUES (
          :job_key, :capture_date, :mode, :status, :total_count, :total_pages,
          :current_page, :opened_rows, :captured_details, :stable_rounds,
          :failure_count, :last_error, :last_action, :status_payload, :started_at,
          :finished_at
        )
        ON CONFLICT(job_key) DO UPDATE SET
          capture_date = excluded.capture_date,
          mode = excluded.mode,
          status = excluded.status,
          total_count = COALESCE(excluded.total_count, reception_chatlog_capture_jobs.total_count),
          total_pages = COALESCE(excluded.total_pages, reception_chatlog_capture_jobs.total_pages),
          current_page = COALESCE(excluded.current_page, reception_chatlog_capture_jobs.current_page),
          opened_rows = COALESCE(excluded.opened_rows, reception_chatlog_capture_jobs.opened_rows),
          captured_details = COALESCE(excluded.captured_details, reception_chatlog_capture_jobs.captured_details),
          stable_rounds = COALESCE(excluded.stable_rounds, reception_chatlog_capture_jobs.stable_rounds),
          failure_count = COALESCE(excluded.failure_count, reception_chatlog_capture_jobs.failure_count),
          last_error = COALESCE(excluded.last_error, reception_chatlog_capture_jobs.last_error),
          last_action = COALESCE(excluded.last_action, reception_chatlog_capture_jobs.last_action),
          status_payload = COALESCE(excluded.status_payload, reception_chatlog_capture_jobs.status_payload),
          started_at = COALESCE(excluded.started_at, reception_chatlog_capture_jobs.started_at),
          finished_at = CASE
            WHEN excluded.status IN ('finished', 'failed', 'stopped') THEN excluded.finished_at
            ELSE NULL
          END,
          updated_at = CURRENT_TIMESTAMP
        """,
        values,
    )
    return get_reception_capture_job(conn, job_key) or values


def get_reception_capture_job(conn: sqlite3.Connection, job_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT job_key, capture_date, mode, status, total_count, total_pages,
               current_page, opened_rows, captured_details, stable_rounds, failure_count,
               last_error, last_action, started_at, finished_at, created_at, updated_at
        FROM reception_chatlog_capture_jobs
        WHERE job_key = ?
        """,
        (job_key,),
    ).fetchone()
    return dict(row) if row else None


def list_reception_capture_jobs(
    conn: sqlite3.Connection,
    limit: int,
    *,
    capture_date: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if capture_date:
        where = "WHERE capture_date = ?"
        params.append(capture_date)
    rows = conn.execute(
        f"""
        SELECT job_key, capture_date, mode, status, total_count, total_pages,
               current_page, opened_rows, captured_details, stable_rounds, failure_count,
               last_error, last_action, started_at, finished_at, created_at, updated_at
        FROM reception_chatlog_capture_jobs
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_reception_capture_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = list_reception_capture_jobs(conn, 1)
    return rows[0] if rows else None


def list_reception_chatlog_sessions(
    conn: sqlite3.Connection,
    limit: int,
    *,
    offset: int = 0,
    q: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    waiter: str | None = None,
    goods_id: str | None = None,
    keyword: str | None = None,
    result_tag: str | None = None,
) -> list[dict[str, Any]]:
    where_sql, params = reception_chatlog_session_filters(
        q=q,
        source=source,
        date_from=date_from,
        date_to=date_to,
        customer=customer,
        waiter=waiter,
        goods_id=goods_id,
        keyword=keyword,
        result_tag=result_tag,
    )
    rows = conn.execute(
        f"""
        SELECT s.conversation_key, s.cid_hash, s.mall_id, s.mall_name, s.group_id, s.group_name,
               s.customer_hash, s.waiter_hash, s.session_type, s.session_type_desc,
               s.customer_display_id, s.waiter_display_id, s.transfer_waiter_display_id,
               s.result_tags, s.consultation_type, s.consultation_at, s.allocate_at,
               s.goods_id, s.goods_name, s.new_response_avg_seconds, s.first_response_at,
               s.session_duration_minutes, s.evaluation_source, s.evaluation_at,
               s.dissatisfied_reason, s.intent_primary, s.intent_secondary, s.scene,
               s.last_msg_id, s.last_mid, s.last_message_at,
               s.updated_at, COUNT(m.id) AS message_count, MAX(m.captured_at) AS last_captured_at,
               SUM(CASE WHEN m.direction = 'customer_or_external' THEN 1 ELSE 0 END) AS customer_message_count,
               SUM(CASE WHEN m.direction = 'seller_or_waiter' THEN 1 ELSE 0 END) AS waiter_message_count,
               GROUP_CONCAT(DISTINCT m.source) AS sources
        FROM reception_chatlog_sessions s
        LEFT JOIN reception_chatlog_messages m ON m.conversation_key = s.conversation_key
        {where_sql}
        GROUP BY s.conversation_key
        ORDER BY COALESCE(s.last_message_at, MAX(m.captured_at), s.updated_at) DESC
        LIMIT ?
        OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def count_reception_chatlog_sessions(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    waiter: str | None = None,
    goods_id: str | None = None,
    keyword: str | None = None,
    result_tag: str | None = None,
) -> int:
    where_sql, params = reception_chatlog_session_filters(
        q=q,
        source=source,
        date_from=date_from,
        date_to=date_to,
        customer=customer,
        waiter=waiter,
        goods_id=goods_id,
        keyword=keyword,
        result_tag=result_tag,
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM reception_chatlog_sessions s
        {where_sql}
        """,
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def reception_chatlog_session_filters(
    *,
    q: str | None = None,
    source: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    customer: str | None = None,
    waiter: str | None = None,
    goods_id: str | None = None,
    keyword: str | None = None,
    result_tag: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    session_time = "COALESCE(s.consultation_at, s.last_message_at, s.captured_at)"
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            """
            (
              s.conversation_key LIKE ?
              OR s.cid_hash LIKE ?
              OR s.mall_id LIKE ?
              OR s.mall_name LIKE ?
              OR s.customer_display_id LIKE ?
              OR s.waiter_display_id LIKE ?
              OR s.transfer_waiter_display_id LIKE ?
              OR s.customer_hash LIKE ?
              OR s.waiter_hash LIKE ?
              OR s.goods_id LIKE ?
              OR s.goods_name LIKE ?
              OR s.group_name LIKE ?
              OR s.result_tags LIKE ?
            )
            """
        )
        params.extend([like] * 13)

    if date_from:
        conditions.append(f"date({session_time}, '+8 hours') >= date(?)")
        params.append(date_from)
    if date_to:
        conditions.append(f"date({session_time}, '+8 hours') <= date(?)")
        params.append(date_to)

    if customer:
        like = f"%{customer.strip()}%"
        conditions.append("(s.customer_display_id LIKE ? OR s.customer_hash LIKE ?)")
        params.extend([like, like])

    if waiter:
        like = f"%{waiter.strip()}%"
        conditions.append(
            """
            (
              s.waiter_display_id LIKE ?
              OR s.transfer_waiter_display_id LIKE ?
              OR s.waiter_hash LIKE ?
            )
            """
        )
        params.extend([like, like, like])

    if goods_id:
        like = f"%{goods_id.strip()}%"
        conditions.append("s.goods_id LIKE ?")
        params.append(like)

    if result_tag:
        like = f"%{result_tag.strip()}%"
        conditions.append("s.result_tags LIKE ?")
        params.append(like)

    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(
            """
            (
              s.goods_name LIKE ?
              OR EXISTS (
                SELECT 1
                FROM reception_chatlog_messages keyword_messages
                WHERE keyword_messages.conversation_key = s.conversation_key
                  AND keyword_messages.content LIKE ?
              )
            )
            """
        )
        params.extend([like, like])

    if source:
        conditions.append(
            """
            EXISTS (
              SELECT 1
              FROM reception_chatlog_messages source_messages
              WHERE source_messages.conversation_key = s.conversation_key
                AND instr(',' || source_messages.source || ',', ',' || ? || ',') > 0
            )
            """
        )
        params.append(source)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_sql, params


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
               media_url, media_width, media_height, message_at, lang, from_display_id,
               to_display_id, source, captured_at, updated_at
        FROM reception_chatlog_messages
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(message_at, captured_at) {order_sql}, id {id_order_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_reception_chatlog_customers(
    conn: sqlite3.Connection,
    limit: int,
    *,
    offset: int = 0,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    where_sql, params = reception_chatlog_customer_filters(q=q, date_from=date_from, date_to=date_to)
    rows = conn.execute(
        f"""
        SELECT s.customer_hash,
               COALESCE(MAX(s.customer_display_id), substr(s.customer_hash, 1, 12)) AS customer_display_id,
               COUNT(DISTINCT s.conversation_key) AS session_count,
               COUNT(m.id) AS message_count,
               MIN(COALESCE(s.consultation_at, s.last_message_at, s.captured_at)) AS first_session_at,
               MAX(COALESCE(s.consultation_at, s.last_message_at, s.captured_at)) AS last_session_at,
               GROUP_CONCAT(DISTINCT s.group_name) AS group_names,
               GROUP_CONCAT(DISTINCT s.waiter_display_id) AS waiter_display_ids
        FROM reception_chatlog_sessions s
        LEFT JOIN reception_chatlog_messages m ON m.conversation_key = s.conversation_key
        {where_sql}
        GROUP BY s.customer_hash
        ORDER BY MAX(COALESCE(s.last_message_at, s.consultation_at, s.captured_at)) DESC
        LIMIT ?
        OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def count_reception_chatlog_customers(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where_sql, params = reception_chatlog_customer_filters(q=q, date_from=date_from, date_to=date_to)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM (
          SELECT s.customer_hash
          FROM reception_chatlog_sessions s
          {where_sql}
          GROUP BY s.customer_hash
        ) grouped_customers
        """,
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def reception_chatlog_customer_filters(
    *,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = ["s.customer_hash IS NOT NULL"]
    params: list[Any] = []
    session_time = "COALESCE(s.consultation_at, s.last_message_at, s.captured_at)"

    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            """
            (
              s.customer_display_id LIKE ?
              OR s.customer_hash LIKE ?
              OR s.group_name LIKE ?
              OR s.goods_name LIKE ?
              OR s.waiter_display_id LIKE ?
            )
            """
        )
        params.extend([like] * 5)
    if date_from:
        conditions.append(f"date({session_time}, '+8 hours') >= date(?)")
        params.append(date_from)
    if date_to:
        conditions.append(f"date({session_time}, '+8 hours') <= date(?)")
        params.append(date_to)

    return f"WHERE {' AND '.join(conditions)}", params


def list_reception_chatlog_customer_messages(
    conn: sqlite3.Connection,
    customer_hash: str,
    limit: int,
    *,
    order: str = "asc",
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    order_sql = "ASC" if order == "asc" else "DESC"
    conditions = ["s.customer_hash = ?"]
    params: list[Any] = [customer_hash]
    session_time = "COALESCE(s.consultation_at, s.last_message_at, s.captured_at)"

    if date_from:
        conditions.append(f"date({session_time}, '+8 hours') >= date(?)")
        params.append(date_from)
    if date_to:
        conditions.append(f"date({session_time}, '+8 hours') <= date(?)")
        params.append(date_to)
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append("(m.content LIKE ? OR s.goods_name LIKE ?)")
        params.extend([like, like])

    rows = conn.execute(
        f"""
        SELECT m.dedupe_key, m.msg_id, m.mid, m.local_id, m.direction, m.body_type,
               m.content, m.content_hash, m.media_url, m.media_width, m.media_height,
               m.message_at, m.lang, m.from_display_id, m.to_display_id, m.source,
               m.captured_at, m.updated_at, s.conversation_key, s.customer_hash,
               s.customer_display_id, s.waiter_display_id, s.transfer_waiter_display_id,
               s.consultation_at, s.consultation_type, s.result_tags, s.group_name,
               s.goods_id, s.goods_name
        FROM reception_chatlog_messages m
        JOIN reception_chatlog_sessions s ON s.conversation_key = m.conversation_key
        WHERE {" AND ".join(conditions)}
        ORDER BY COALESCE(s.consultation_at, m.message_at, m.captured_at) {order_sql},
                 COALESCE(m.message_at, m.captured_at) {order_sql},
                 m.id {order_sql}
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
    event_sources = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM reception_chatlog_events
        GROUP BY source
        ORDER BY count DESC, source
        """
    ).fetchall()
    return {
        "totals": dict(totals) if totals else {},
        "latest_message": dict(latest_message) if latest_message else None,
        "event_sources": [dict(row) for row in event_sources],
        "latest_capture_job": latest_reception_capture_job(conn),
    }


def list_reception_chatlog_events_recent(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id, source, event_type, conversation_key, message_dedupe_key,
               captured_at, received_at
        FROM reception_chatlog_events
        ORDER BY received_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
