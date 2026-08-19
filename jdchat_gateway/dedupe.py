from __future__ import annotations

import hashlib


def sha256_text(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def content_hash(content: str | None) -> str | None:
    if content is None:
        return None
    return sha256_text(content)


def compute_dedupe_key(
    *,
    platform: str,
    conversation_key: str,
    msg_id: str | None = None,
    mid: int | str | None = None,
    timestamp: int | str | None = None,
    direction: str | None = None,
    body_type: str | None = None,
    content_hash_value: str | None = None,
) -> str:
    if msg_id:
        source = f"msg_id:{platform}:{msg_id}"
    elif mid is not None and str(mid) != "":
        source = f"mid:{conversation_key}:{mid}"
    else:
        source = (
            "fallback:"
            f"{conversation_key}:"
            f"{timestamp or ''}:"
            f"{direction or 'unknown'}:"
            f"{body_type or ''}:"
            f"{content_hash_value or ''}"
        )
    return sha256_text(source)


def hash_identifier(value: str | None) -> str | None:
    if not value:
        return None
    return sha256_text(value)
