from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from jdchat_gateway.dedupe import (
    compute_dedupe_key,
    content_hash,
    hash_identifier,
    sha256_text,
)

DONGDONG_PLATFORM = "jd_dongdong"
RECEPTION_PLATFORM = "jd_jingmai_reception"
PLATFORM = DONGDONG_PLATFORM
RECEPTION_SOURCES = {"reception_list", "reception_chatlog", "reception_dom"}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1

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
LOCAL_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


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


def to_sqlite_int(value: Any) -> int | None:
    parsed = to_int(value)
    if parsed is None or parsed < SQLITE_INT_MIN or parsed > SQLITE_INT_MAX:
        return None
    return parsed


def timestamp_to_iso(value: Any) -> str | None:
    ts = to_int(value)
    if ts is not None:
        if ts > 10_000_000_000:
            seconds = ts / 1000
        else:
            seconds = ts
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


def platform_for_source(source: str | None) -> str:
    return RECEPTION_PLATFORM if source in RECEPTION_SOURCES else DONGDONG_PLATFORM


def is_reception_source(source: str | None) -> bool:
    return platform_for_source(source) == RECEPTION_PLATFORM


def event_payload_message(event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message")
    if isinstance(message, dict):
        return message
    payload = event.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("message") or payload.get("msg") or payload.get("chatLogMessage") or payload.get("data")
        if isinstance(nested, dict):
            return nested
        if "body" in payload or "from" in payload or "to" in payload:
            return payload
    return None


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def normalize_reception_conversation(
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
    customer_value = (
        pick(conversation, "customer", "customer_pin", "customerPin", "customerName", "customer_name")
        or pick(message, "customer")
    )
    waiter_value = (
        pick(conversation, "waiter", "service", "service_pin", "servicePin", "seller_pin", "sellerPin")
        or pick(message, "waiter")
        or pick(batch, "waiterAccountHash")
    )

    customer_pin_hash = (
        pick(conversation, "customer_pin_hash", "customerPinHash", "customer_hash", "customerHash")
        or (hash_identifier(str(customer_value)) if customer_value else None)
    )
    seller_pin_hash = (
        pick(conversation, "seller_pin_hash", "sellerPinHash", "waiter_hash", "waiterHash", "service_hash", "serviceHash")
        or (waiter_value if pick(batch, "waiterAccountHash") == waiter_value else None)
        or (hash_identifier(str(waiter_value)) if waiter_value else None)
    )
    vender_id = (
        pick(conversation, "mall_id", "mallId", "vender_id", "venderId", "shop_id", "shopId")
        or pick(batch, "shopId")
        or pick(message, "mallId", "mall_id")
    )
    vender_name = pick(conversation, "mall_name", "mallName", "vender_name", "venderName")
    session_type = str(
        pick(conversation, "session_type", "sessionTypeDesc", "sessionType")
        or pick(message, "sessionTypeDesc", "sessionType")
        or "reception_chatlog"
    )

    conversation_key = pick(conversation, "conversation_key", "conversationKey")
    if not conversation_key:
        identity = cid_hash or f"{vender_id or ''}:{customer_pin_hash or ''}:{seller_pin_hash or ''}:{session_type}"
        conversation_key = sha256_text(f"{RECEPTION_PLATFORM}:{identity}")

    raw_snapshot = {**conversation}
    if cid_hash and "cidHash" not in raw_snapshot and "cid_hash" not in raw_snapshot:
        raw_snapshot["cidHash"] = cid_hash

    return {
        "platform": RECEPTION_PLATFORM,
        "conversation_key": conversation_key,
        "vender_id": vender_id,
        "vender_name": vender_name,
        "seller_app": "jingmai.waiter",
        "seller_pin_hash": seller_pin_hash,
        "customer_app": "jingmai.customer",
        "customer_pin_hash": customer_pin_hash,
        "customer_name": pick(conversation, "customer_name", "customerName", "name"),
        "session_type": session_type,
        "last_read_mid": to_sqlite_int(pick(conversation, "last_read_mid", "lastReadMid")),
        "unread_count": to_sqlite_int(pick(conversation, "unread_count", "unreadCount")),
        "raw_customer": compact_json(redact_sensitive(raw_snapshot)),
    }


def normalize_conversation(
    *,
    event: dict[str, Any],
    message: dict[str, Any] | None,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = event.get("source")
    platform = platform_for_source(source)
    if platform == RECEPTION_PLATFORM:
        return normalize_reception_conversation(event=event, message=message, batch=batch)

    conversation = dict(event.get("conversation") or {})
    body = message.get("body") if isinstance(message, dict) and isinstance(message.get("body"), dict) else {}
    chatinfo = body.get("chatinfo") if isinstance(body.get("chatinfo"), dict) else {}
    param = body.get("param") if isinstance(body.get("param"), dict) else {}
    customer = event.get("customer") if isinstance(event.get("customer"), dict) else {}

    from_party = message.get("from") if isinstance(message, dict) and isinstance(message.get("from"), dict) else {}
    to_party = message.get("to") if isinstance(message, dict) and isinstance(message.get("to"), dict) else {}

    vender_id = pick(conversation, "vender_id", "venderId") or pick(chatinfo, "venderId") or pick(param, "venderId")
    vender_name = (
        pick(conversation, "vender_name", "venderName")
        or pick(chatinfo, "venderName")
        or pick(param, "venderName")
    )

    seller_app = pick(conversation, "seller_app", "sellerApp")
    seller_pin = pick(conversation, "seller_pin", "sellerPin")
    customer_app = pick(conversation, "customer_app", "customerApp", "app") or pick(customer, "app")
    customer_pin = pick(conversation, "customer_pin", "customerPin", "pin") or pick(customer, "pin")

    if not seller_app and customer_app:
        if from_party.get("app") and from_party.get("app") != customer_app:
            seller_app = from_party.get("app")
        elif to_party.get("app") and to_party.get("app") != customer_app:
            seller_app = to_party.get("app")

    if not customer_app and seller_app:
        if from_party.get("app") and from_party.get("app") != seller_app:
            customer_app = from_party.get("app")
        elif to_party.get("app") and to_party.get("app") != seller_app:
            customer_app = to_party.get("app")

    if not customer_app:
        customer_app = pick(from_party, "app") or "unknown_customer_app"
    if not customer_pin:
        customer_pin = pick(from_party, "pin") or "unknown_customer_pin"

    session_type = str(pick(conversation, "session_type", "sessionType") or pick(customer, "sessionType") or "unknown")
    customer_pin_hash = pick(conversation, "customer_pin_hash", "customerPinHash") or hash_identifier(str(customer_pin))
    seller_pin_hash = pick(conversation, "seller_pin_hash", "sellerPinHash")
    if not seller_pin_hash and seller_pin:
        seller_pin_hash = hash_identifier(str(seller_pin))

    conversation_key = pick(conversation, "conversation_key", "conversationKey")
    if not conversation_key:
        conversation_key = sha256_text(
            f"{platform}:{vender_id or ''}:{customer_app}:{customer_pin_hash or ''}:{session_type}"
        )

    return {
        "platform": platform,
        "conversation_key": conversation_key,
        "vender_id": vender_id,
        "vender_name": vender_name,
        "seller_app": seller_app,
        "seller_pin_hash": seller_pin_hash,
        "customer_app": customer_app,
        "customer_pin_hash": customer_pin_hash,
        "customer_name": pick(conversation, "customer_name", "customerName", "name") or pick(customer, "name"),
        "session_type": session_type,
        "last_read_mid": to_sqlite_int(
            pick(conversation, "last_read_mid", "lastReadMid") or pick(customer, "lastReadMid")
        ),
        "unread_count": to_sqlite_int(pick(conversation, "unread_count", "unreadCount") or pick(customer, "unreadCount")),
        "raw_customer": compact_json(redact_sensitive(customer or conversation)),
    }


def infer_direction(message: dict[str, Any], conversation: dict[str, Any]) -> str:
    from_party = message.get("from") if isinstance(message.get("from"), dict) else {}
    to_party = message.get("to") if isinstance(message.get("to"), dict) else {}
    customer_app = conversation.get("customer_app")
    customer_pin_hash = conversation.get("customer_pin_hash")
    from_pin_hash = hash_identifier(from_party.get("pin"))
    to_pin_hash = hash_identifier(to_party.get("pin"))

    if customer_app and from_party.get("app") == customer_app:
        return "customer_or_external"
    if customer_pin_hash and from_pin_hash == customer_pin_hash:
        return "customer_or_external"
    if customer_app and to_party.get("app") == customer_app:
        return "seller_or_waiter"
    if customer_pin_hash and to_pin_hash == customer_pin_hash:
        return "seller_or_waiter"
    return "unknown"


def normalize_reception_message(
    *,
    event: dict[str, Any],
    message: dict[str, Any],
    conversation: dict[str, Any],
) -> dict[str, Any]:
    cid_hash = pick(event.get("conversation") or {}, "cid_hash", "cidHash", "conversationCidHash")
    if not cid_hash:
        conversation_key = conversation.get("conversation_key") or ""
        cid_hash = conversation_key[:16] if conversation_key else None

    raw_mid = pick(message, "mid")
    uuid = pick(message, "uuid")
    sid = pick(message, "sid")
    explicit_msg_id = pick(message, "id", "msgId", "msg_id")
    if explicit_msg_id:
        msg_id = str(explicit_msg_id)
    elif raw_mid not in (None, "") and cid_hash:
        msg_id = f"jm:{cid_hash}:{raw_mid}"
    elif uuid and cid_hash:
        msg_id = f"jm:{cid_hash}:{uuid}"
    elif uuid:
        msg_id = f"jm:uuid:{uuid}"
    else:
        msg_id = None

    media_url = pick(message, "imgUrl", "img_url", "mediaUrl", "media_url")
    body_type = "image" if media_url else str(pick(message, "bodyType", "body_type", "type") or "text")
    content = pick(message, "content", "msg", "text")
    if content is not None:
        content = str(content)

    waiter_send = boolish(pick(message, "waiterSend", "waiter_send"))
    direction = "seller_or_waiter" if waiter_send else "customer_or_external"
    message_at = timestamp_to_iso(pick(message, "created", "messageAt", "message_at", "time"))
    customer = pick(message, "customer")
    waiter = pick(message, "waiter")
    customer_hash = pick(event.get("conversation") or {}, "customer_pin_hash", "customerPinHash", "customerHash")
    customer_hash = customer_hash or (hash_identifier(str(customer)) if customer else conversation.get("customer_pin_hash"))
    waiter_hash = pick(event.get("conversation") or {}, "seller_pin_hash", "sellerPinHash", "waiterHash", "serviceHash")
    waiter_hash = waiter_hash or (hash_identifier(str(waiter)) if waiter else conversation.get("seller_pin_hash"))
    from_hash = waiter_hash if waiter_send else customer_hash
    to_hash = customer_hash if waiter_send else waiter_hash
    msg_content_hash = content_hash(content)
    mid = to_sqlite_int(raw_mid)
    dedupe_key = compute_dedupe_key(
        platform=RECEPTION_PLATFORM,
        conversation_key=conversation["conversation_key"],
        msg_id=msg_id,
        mid=raw_mid if raw_mid not in (None, "") else None,
        timestamp=message_at or pick(message, "created", "messageAt", "message_at", "time"),
        direction=direction,
        body_type=body_type,
        content_hash_value=msg_content_hash,
    )
    captured_at = pick(event, "captured_at", "capturedAt") or now_iso()

    return {
        "dedupe_key": dedupe_key,
        "platform": RECEPTION_PLATFORM,
        "conversation_key": conversation["conversation_key"],
        "msg_id": msg_id,
        "mid": mid,
        "local_id": str(sid) if sid not in (None, "") else None,
        "direction": direction,
        "top_type": "jingmai_chat_log",
        "body_type": body_type,
        "content": content,
        "content_hash": msg_content_hash,
        "media_url": media_url,
        "media_local_path": pick(message, "localPath", "local_path", "mediaLocalPath", "media_local_path"),
        "media_mime_type": pick(message, "mimeType", "mime_type", "mediaMimeType", "media_mime_type"),
        "media_storage_provider": pick(message, "storageProvider", "storage_provider", "mediaStorageProvider"),
        "media_download_status": pick(message, "downloadStatus", "download_status", "mediaDownloadStatus"),
        "media_download_error": pick(message, "downloadError", "download_error", "mediaDownloadError"),
        "media_width": to_sqlite_int(pick(message, "width")),
        "media_height": to_sqlite_int(pick(message, "height")),
        "media_size": to_sqlite_int(pick(message, "size")),
        "template_type": None,
        "template_payload": None,
        "message_at": message_at,
        "client_time": None,
        "datetime_ms": None,
        "timestamp_ms": None,
        "read_flag": None,
        "state": None,
        "lang": pick(message, "lang"),
        "from_app": "jingmai.waiter" if waiter_send else "jingmai.customer",
        "from_pin_hash": from_hash,
        "from_client_type": None,
        "from_art": None,
        "to_app": "jingmai.customer" if waiter_send else "jingmai.waiter",
        "to_pin_hash": to_hash,
        "source": event["source"],
        "raw_json": compact_json(redact_sensitive(message)),
        "captured_at": captured_at,
    }


def normalize_message(
    *,
    event: dict[str, Any],
    message: dict[str, Any],
    conversation: dict[str, Any],
) -> dict[str, Any]:
    source = event.get("source")
    platform = platform_for_source(source)
    if platform == RECEPTION_PLATFORM:
        return normalize_reception_message(event=event, message=message, conversation=conversation)

    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    from_party = message.get("from") if isinstance(message.get("from"), dict) else {}
    to_party = message.get("to") if isinstance(message.get("to"), dict) else {}
    timestamp = pick(message, "timestamp", "datetime", "clientTime", "time")
    body_type = pick(body, "type")
    content = pick(body, "content") or pick(message, "content", "msg", "text")
    if content is not None:
        content = str(content)
    media_url = pick(body, "url", "mediaUrl", "media_url")
    msg_content_hash = content_hash(content)
    msg_id = pick(message, "id", "msgId", "msg_id")
    raw_mid = pick(message, "mid")
    mid = to_sqlite_int(raw_mid)
    direction = pick(message, "direction") or infer_direction(message, conversation)
    dedupe_msg_id = msg_id
    dedupe_timestamp = timestamp
    dedupe_content_hash = msg_content_hash
    if event["source"] == "dom" and isinstance(msg_id, str) and msg_id.startswith("dom-"):
        dedupe_msg_id = None
        dedupe_timestamp = timestamp or pick(message, "displayTime", "display_time")
        dedupe_content_hash = content_hash(compact_json({"content": content, "media_url": media_url}))

    dedupe_key = compute_dedupe_key(
        platform=platform,
        conversation_key=conversation["conversation_key"],
        msg_id=dedupe_msg_id,
        mid=mid if mid is not None else raw_mid,
        timestamp=dedupe_timestamp,
        direction=direction,
        body_type=body_type,
        content_hash_value=dedupe_content_hash,
    )

    captured_at = pick(event, "captured_at", "capturedAt") or now_iso()

    return {
        "dedupe_key": dedupe_key,
        "platform": platform,
        "conversation_key": conversation["conversation_key"],
        "msg_id": msg_id,
        "mid": mid,
        "local_id": pick(message, "localId", "local_id", "localMid"),
        "direction": direction,
        "top_type": pick(message, "type"),
        "body_type": body_type,
        "content": content,
        "content_hash": msg_content_hash,
        "media_url": media_url,
        "media_local_path": pick(body, "localPath", "local_path", "mediaLocalPath", "media_local_path"),
        "media_mime_type": pick(body, "mimeType", "mime_type", "mediaMimeType", "media_mime_type"),
        "media_storage_provider": pick(body, "storageProvider", "storage_provider", "mediaStorageProvider"),
        "media_download_status": pick(body, "downloadStatus", "download_status", "mediaDownloadStatus"),
        "media_download_error": pick(body, "downloadError", "download_error", "mediaDownloadError"),
        "media_width": to_int(pick(body, "width")),
        "media_height": to_int(pick(body, "height")),
        "media_size": to_int(pick(body, "size")),
        "template_type": pick(body.get("template", {}) if isinstance(body.get("template"), dict) else {}, "nativeId")
        or ("template2" if body_type == "template2" else None),
        "template_payload": compact_json(redact_sensitive(body.get("template") or body.get("data"))),
        "message_at": timestamp_to_iso(timestamp),
        "client_time": to_sqlite_int(pick(message, "clientTime")),
        "datetime_ms": to_sqlite_int(pick(message, "datetime")),
        "timestamp_ms": to_sqlite_int(pick(message, "timestamp")),
        "read_flag": to_sqlite_int(pick(message, "readFlag", "read_flag")),
        "state": to_sqlite_int(pick(message, "state")),
        "lang": pick(message, "lang"),
        "from_app": pick(from_party, "app"),
        "from_pin_hash": hash_identifier(pick(from_party, "pin")),
        "from_client_type": pick(from_party, "clientType", "client_type"),
        "from_art": pick(from_party, "art"),
        "to_app": pick(to_party, "app"),
        "to_pin_hash": hash_identifier(pick(to_party, "pin")),
        "source": event["source"],
        "raw_json": compact_json(redact_sensitive(message)),
        "captured_at": captured_at,
    }


def normalize_capture_event(event: dict[str, Any], batch: dict[str, Any] | None = None) -> dict[str, Any]:
    message = event_payload_message(event)
    conversation = normalize_conversation(event=event, message=message, batch=batch)
    normalized_message = None
    if message:
        normalized_message = normalize_message(event=event, message=message, conversation=conversation)

    event_id = pick(event, "event_id", "eventId")
    if not event_id:
        stable_basis = compact_json(
            {
                "source": event.get("source"),
                "event_type": event.get("event_type") or event.get("eventType"),
                "conversation_key": conversation["conversation_key"],
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
        "conversation": conversation,
        "message": normalized_message,
        "payload": compact_json(redact_sensitive(event)),
        "captured_at": pick(event, "captured_at", "capturedAt") or now_iso(),
    }
