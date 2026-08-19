from __future__ import annotations

import base64
import binascii
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from jdchat_gateway.dedupe import sha256_text
from jdchat_gateway.settings import Settings

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


def cache_message_media(message: dict[str, Any], settings: Settings) -> None:
    media_url = message.get("media_url")
    if not media_url:
        return

    provider = settings.media_storage_provider.lower().strip()
    message["media_storage_provider"] = provider
    if not settings.media_download_enabled:
        message["media_download_status"] = "disabled"
        return
    if provider != "local":
        message["media_download_status"] = "unsupported_provider"
        return

    try:
        payload, mime_type = read_media_bytes(
            str(media_url),
            timeout=settings.media_download_timeout_seconds,
            max_bytes=settings.media_download_max_bytes,
        )
        relative_path = local_media_relative_path(message, str(media_url), mime_type)
        target = settings.media_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(target)

        message["media_local_path"] = relative_path.as_posix()
        message["media_mime_type"] = mime_type
        message["media_download_status"] = "saved"
        message["media_download_error"] = None
        if message.get("media_size") in (None, ""):
            message["media_size"] = len(payload)
    except Exception as exc:  # noqa: BLE001 - media failures should not reject chat capture.
        message["media_download_status"] = "failed"
        message["media_download_error"] = str(exc)[:300]


def read_media_bytes(url: str, *, timeout: float, max_bytes: int) -> tuple[bytes, str | None]:
    if url.startswith("data:"):
        return read_data_url(url, max_bytes=max_bytes)

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported media url scheme: {parsed.scheme or 'empty'}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "jdchat-local-capture/0.1",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            mime_type = content_type(response.headers.get("Content-Type"))
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("media file is larger than configured max bytes")
            payload = read_limited(response, max_bytes=max_bytes)
            return payload, mime_type
    except urllib.error.URLError as exc:
        raise ValueError(f"media download failed: {exc.reason}") from exc


def read_data_url(url: str, *, max_bytes: int) -> tuple[bytes, str | None]:
    header, separator, encoded = url.partition(",")
    if not separator:
        raise ValueError("invalid data url")
    mime_type = content_type(header[5:].split(";", 1)[0] or None)
    try:
        if ";base64" in header:
            payload = base64.b64decode(encoded, validate=True)
        else:
            payload = urllib.parse.unquote_to_bytes(encoded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid data url payload") from exc
    if len(payload) > max_bytes:
        raise ValueError("media file is larger than configured max bytes")
    return payload, mime_type


def read_limited(response: Any, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("media file is larger than configured max bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def local_media_relative_path(message: dict[str, Any], media_url: str, mime_type: str | None) -> Path:
    conversation_key = str(message.get("conversation_key") or "unknown")
    dedupe_key = str(message.get("dedupe_key") or sha256_text(media_url))
    return Path(conversation_key[:16]) / f"{dedupe_key[:24]}{media_extension(media_url, mime_type)}"


def media_extension(url: str, mime_type: str | None) -> str:
    if mime_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[mime_type]
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
        return suffix
    return ".bin"


def content_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def media_public_url(relative_path: str | None, settings: Settings) -> str | None:
    if not relative_path:
        return None
    base = settings.media_public_base_url
    if base:
        return f"{base.rstrip('/')}/{urllib.parse.quote(relative_path)}"
    return f"/media/{urllib.parse.quote(relative_path)}"
