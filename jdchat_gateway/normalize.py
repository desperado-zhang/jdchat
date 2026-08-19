from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from jdchat_gateway.dedupe import (
    compute_dedupe_key,
    content_hash,
    hash_identifier,
    sha256_text,
)

PLATFORM = "jd_dongdong"

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


def timestamp_to_iso(value: Any) -> str | None:
    ts = to_int(value)
    if ts is None:
        return None
    if ts > 10_000_000_000:
        seconds = ts / 1000
    else:
        seconds = ts
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def compact_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def redact_sensitive(value: Any, key: str = "") -> Any:
    key_lower = key.lower()
    if isinstance(value, dict):
        return {k: redact_sensitive(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item, key) for item in value]
    if isinstance(value, str) and any(part in key_lower for part in SENSITIVE_KEY_FRAGMENTS):
        return {"redacted": True, "len": len(value), "hash": sha256_text(value)}
    return value


def event_payload_message(event: dict[str, Any]) -> dict[str, Any] | None:
    message = event.get("message")
    if isinstance(message, dict):
        return message
    payload = event.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("message") or payload.get("msg") or payload.get("data")
        if isinstance(nested, dict):
            return nested
        if "body" in payload or "from" in payload or "to" in payload:
            return payload
    return None


def normalize_conversation(
    *,
    event: dict[str, Any],
    message: dict[str, Any] | None,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    customer_app = pick(conversation, "customer_app", "customerApp") or pick(customer, "app")
    customer_pin = pick(conversation, "customer_pin", "customerPin") or pick(customer, "pin")

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
            f"{PLATFORM}:{vender_id or ''}:{customer_app}:{customer_pin_hash or ''}:{session_type}"
        )

    return {
        "platform": PLATFORM,
        "conversation_key": conversation_key,
        "vender_id": vender_id,
        "vender_name": vender_name,
        "seller_app": seller_app,
        "seller_pin_hash": seller_pin_hash,
        "customer_app": customer_app,
        "customer_pin_hash": customer_pin_hash,
        "customer_name": pick(conversation, "customer_name", "customerName") or pick(customer, "name"),
        "session_type": session_type,
        "last_read_mid": to_int(pick(conversation, "last_read_mid", "lastReadMid") or pick(customer, "lastReadMid")),
        "unread_count": to_int(pick(conversation, "unread_count", "unreadCount") or pick(customer, "unreadCount")),
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


def normalize_message(
    *,
    event: dict[str, Any],
    message: dict[str, Any],
    conversation: dict[str, Any],
) -> dict[str, Any]:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    from_party = message.get("from") if isinstance(message.get("from"), dict) else {}
    to_party = message.get("to") if isinstance(message.get("to"), dict) else {}
    timestamp = pick(message, "timestamp", "datetime", "clientTime", "time")
    body_type = pick(body, "type")
    content = pick(body, "content") or pick(message, "content", "msg", "text")
    if content is not None:
        content = str(content)
    msg_content_hash = content_hash(content)
    msg_id = pick(message, "id", "msgId", "msg_id")
    mid = to_int(pick(message, "mid"))
    direction = pick(message, "direction") or infer_direction(message, conversation)

    dedupe_key = compute_dedupe_key(
        platform=PLATFORM,
        conversation_key=conversation["conversation_key"],
        msg_id=msg_id,
        mid=mid,
        timestamp=timestamp,
        direction=direction,
        body_type=body_type,
        content_hash_value=msg_content_hash,
    )

    captured_at = pick(event, "captured_at", "capturedAt") or now_iso()

    return {
        "dedupe_key": dedupe_key,
        "platform": PLATFORM,
        "conversation_key": conversation["conversation_key"],
        "msg_id": msg_id,
        "mid": mid,
        "local_id": pick(message, "localId", "local_id", "localMid"),
        "direction": direction,
        "top_type": pick(message, "type"),
        "body_type": body_type,
        "content": content,
        "content_hash": msg_content_hash,
        "media_url": pick(body, "url", "mediaUrl", "media_url"),
        "media_width": to_int(pick(body, "width")),
        "media_height": to_int(pick(body, "height")),
        "media_size": to_int(pick(body, "size")),
        "template_type": pick(body.get("template", {}) if isinstance(body.get("template"), dict) else {}, "nativeId")
        or ("template2" if body_type == "template2" else None),
        "template_payload": compact_json(redact_sensitive(body.get("template") or body.get("data"))),
        "message_at": timestamp_to_iso(timestamp),
        "client_time": to_int(pick(message, "clientTime")),
        "datetime_ms": to_int(pick(message, "datetime")),
        "timestamp_ms": to_int(pick(message, "timestamp")),
        "read_flag": to_int(pick(message, "readFlag", "read_flag")),
        "state": to_int(pick(message, "state")),
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
