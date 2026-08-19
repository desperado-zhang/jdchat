from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from jdchat_gateway import __version__
from jdchat_gateway.db import connect, init_db
from jdchat_gateway.media import cache_message_media, media_public_url
from jdchat_gateway.models import CaptureBatchIn, CaptureRejected, CaptureResponse, HealthResponse
from jdchat_gateway.normalize import normalize_capture_event
from jdchat_gateway.reception import (
    ReceptionCaptureRejected,
    ReceptionCaptureResponse,
    ReceptionChatLogBatchIn,
    count_reception_chatlog_customers,
    count_reception_chatlog_sessions,
    list_reception_chatlog_customer_messages,
    list_reception_chatlog_customers,
    list_reception_chatlog_events_recent,
    list_reception_chatlog_messages,
    list_reception_chatlog_sessions,
    normalize_reception_chatlog_event,
    reception_chatlog_stats,
    record_reception_chatlog_event,
    upsert_reception_chatlog_message,
    upsert_reception_chatlog_session,
)
from jdchat_gateway.repositories import (
    capture_stats,
    count_conversations,
    list_capture_events_recent,
    list_conversations,
    list_messages,
    record_capture_event,
    reuse_existing_message_identity,
    upsert_conversation,
    upsert_message,
)
from jdchat_gateway.settings import Settings

VIEWER_HTML = Path(__file__).with_name("static") / "viewer.html"


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(app_settings.database_path)
        init_db(conn)
        conn.close()
        yield

    app = FastAPI(title="jdchat local capture gateway", version=__version__, lifespan=lifespan)
    app.state.settings = app_settings

    return register_routes(app)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_local_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.api_token:
        return
    expected = f"Bearer {settings.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid local capture token")


def register_routes(app: FastAPI) -> FastAPI:
    @app.get("/viewer", include_in_schema=False)
    def viewer() -> FileResponse:
        if not VIEWER_HTML.exists():
            raise HTTPException(status_code=404, detail="viewer page not found")
        return FileResponse(VIEWER_HTML)

    @app.get("/media/{media_path:path}", include_in_schema=False)
    def media_file(media_path: str, settings: Annotated[Settings, Depends(get_settings)]) -> FileResponse:
        if settings.media_storage_provider.lower().strip() != "local":
            raise HTTPException(status_code=404, detail="local media storage is disabled")
        base = settings.media_dir.resolve()
        target = (base / media_path).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="media file not found") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media file not found")
        return FileResponse(target)

    @app.get("/health", response_model=HealthResponse)
    def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
        conn = connect(settings.database_path)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return HealthResponse(ok=True, database="ok", version=__version__)

    @app.post("/capture/events", response_model=CaptureResponse, dependencies=[Depends(require_local_token)])
    def capture_events(
        batch: CaptureBatchIn,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> CaptureResponse:
        accepted = inserted = updated = duplicates = 0
        rejected: list[CaptureRejected] = []
        conn = connect(settings.database_path)
        try:
            for event_model in batch.events:
                event = event_model.model_dump(by_alias=True, exclude_none=True)
                try:
                    normalized = normalize_capture_event(
                        event,
                        batch=batch.model_dump(by_alias=True, exclude_none=True),
                    )
                    if not normalized.get("message"):
                        rejected.append(
                            CaptureRejected(eventId=normalized["event_id"], reason="event has no message payload")
                        )
                        continue
                    reuse_existing_message_identity(conn, normalized["message"])
                    cache_message_media(normalized["message"], settings)

                    with conn:
                        upsert_conversation(conn, normalized["conversation"], normalized["message"])
                        status = upsert_message(conn, normalized["message"])
                        record_capture_event(conn, normalized)

                    accepted += 1
                    if status == "inserted":
                        inserted += 1
                    elif status == "updated":
                        updated += 1
                    else:
                        duplicates += 1
                except Exception as exc:  # noqa: BLE001 - response needs per-event rejection.
                    rejected.append(CaptureRejected(eventId=event.get("eventId"), reason=str(exc)))
        finally:
            conn.close()

        return CaptureResponse(
            accepted=accepted,
            inserted=inserted,
            updated=updated,
            duplicates=duplicates,
            rejected=rejected,
        )

    @app.post(
        "/reception/chatlog/events",
        response_model=ReceptionCaptureResponse,
        dependencies=[Depends(require_local_token)],
    )
    def reception_chatlog_events(
        batch: ReceptionChatLogBatchIn,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> ReceptionCaptureResponse:
        accepted = inserted = updated = duplicates = 0
        rejected: list[ReceptionCaptureRejected] = []
        conn = connect(settings.database_path)
        batch_payload = batch.model_dump(by_alias=True, exclude_none=True)
        try:
            for event_model in batch.events:
                event = event_model.model_dump(by_alias=True, exclude_none=True)
                try:
                    normalized = normalize_reception_chatlog_event(event, batch=batch_payload)
                    if not normalized.get("message"):
                        rejected.append(
                            ReceptionCaptureRejected(
                                eventId=normalized["event_id"],
                                reason="event has no reception chatlog message payload",
                            )
                        )
                        continue

                    with conn:
                        upsert_reception_chatlog_session(conn, normalized["session"], normalized["message"])
                        status = upsert_reception_chatlog_message(conn, normalized["message"])
                        record_reception_chatlog_event(conn, normalized)

                    accepted += 1
                    if status == "inserted":
                        inserted += 1
                    elif status == "updated":
                        updated += 1
                    else:
                        duplicates += 1
                except Exception as exc:  # noqa: BLE001 - response needs per-event rejection.
                    rejected.append(ReceptionCaptureRejected(eventId=event.get("eventId"), reason=str(exc)))
        finally:
            conn.close()

        return ReceptionCaptureResponse(
            accepted=accepted,
            inserted=inserted,
            updated=updated,
            duplicates=duplicates,
            rejected=rejected,
        )

    @app.get("/conversations", dependencies=[Depends(require_local_token)])
    def conversations(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        source: str | None = None,
    ) -> dict[str, object]:
        bounded_limit = min(max(limit, 1), 200)
        bounded_offset = max(offset, 0)
        conn = connect(settings.database_path)
        try:
            total = count_conversations(conn, q=q, source=source)
            return {
                "items": list_conversations(conn, bounded_limit, offset=bounded_offset, q=q, source=source),
                "pagination": pagination_meta(limit=bounded_limit, offset=bounded_offset, total=total),
            }
        finally:
            conn.close()

    @app.get("/conversations/{conversation_key}/messages", dependencies=[Depends(require_local_token)])
    def conversation_messages(
        conversation_key: str,
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
        order: str = "desc",
        before: str | None = None,
    ) -> dict[str, object]:
        normalized_order = "asc" if order == "asc" else "desc"
        conn = connect(settings.database_path)
        try:
            items = list_messages(
                conn,
                conversation_key,
                min(max(limit, 1), 500),
                order=normalized_order,
                before=before,
            )
            return {
                "items": [with_media_public_url(item, settings) for item in items],
            }
        finally:
            conn.close()

    @app.get("/reception/chatlog/sessions", dependencies=[Depends(require_local_token)])
    def reception_chatlog_sessions(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
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
    ) -> dict[str, object]:
        bounded_limit = min(max(limit, 1), 200)
        bounded_offset = max(offset, 0)
        conn = connect(settings.database_path)
        try:
            total = count_reception_chatlog_sessions(
                conn,
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
            return {
                "items": list_reception_chatlog_sessions(
                    conn,
                    bounded_limit,
                    offset=bounded_offset,
                    q=q,
                    source=source,
                    date_from=date_from,
                    date_to=date_to,
                    customer=customer,
                    waiter=waiter,
                    goods_id=goods_id,
                    keyword=keyword,
                    result_tag=result_tag,
                ),
                "pagination": pagination_meta(limit=bounded_limit, offset=bounded_offset, total=total),
            }
        finally:
            conn.close()

    @app.get("/reception/chatlog/sessions/{conversation_key}/messages", dependencies=[Depends(require_local_token)])
    def reception_chatlog_messages(
        conversation_key: str,
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
        order: str = "desc",
        before: str | None = None,
    ) -> dict[str, object]:
        normalized_order = "asc" if order == "asc" else "desc"
        conn = connect(settings.database_path)
        try:
            items = list_reception_chatlog_messages(
                conn,
                conversation_key,
                min(max(limit, 1), 500),
                order=normalized_order,
                before=before,
            )
            return {
                "items": [with_media_public_url(item, settings) for item in items],
            }
        finally:
            conn.close()

    @app.get("/reception/chatlog/customers", dependencies=[Depends(require_local_token)])
    def reception_chatlog_customers(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, object]:
        bounded_limit = min(max(limit, 1), 200)
        bounded_offset = max(offset, 0)
        conn = connect(settings.database_path)
        try:
            total = count_reception_chatlog_customers(conn, q=q, date_from=date_from, date_to=date_to)
            return {
                "items": list_reception_chatlog_customers(
                    conn,
                    bounded_limit,
                    offset=bounded_offset,
                    q=q,
                    date_from=date_from,
                    date_to=date_to,
                ),
                "pagination": pagination_meta(limit=bounded_limit, offset=bounded_offset, total=total),
            }
        finally:
            conn.close()

    @app.get("/reception/chatlog/customers/{customer_hash}/messages", dependencies=[Depends(require_local_token)])
    def reception_chatlog_customer_messages(
        customer_hash: str,
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 1000,
        order: str = "asc",
        date_from: str | None = None,
        date_to: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, object]:
        normalized_order = "asc" if order == "asc" else "desc"
        conn = connect(settings.database_path)
        try:
            items = list_reception_chatlog_customer_messages(
                conn,
                customer_hash,
                min(max(limit, 1), 2000),
                order=normalized_order,
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
            )
            return {
                "items": [with_media_public_url(item, settings) for item in items],
            }
        finally:
            conn.close()

    @app.get("/reception/chatlog/stats", dependencies=[Depends(require_local_token)])
    def reception_chatlog_capture_stats(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return reception_chatlog_stats(conn)
        finally:
            conn.close()

    @app.get("/reception/chatlog/events/recent", dependencies=[Depends(require_local_token)])
    def recent_reception_chatlog_events(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 20,
    ) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return {"items": list_reception_chatlog_events_recent(conn, min(max(limit, 1), 200))}
        finally:
            conn.close()

    @app.get("/capture/events/recent", dependencies=[Depends(require_local_token)])
    def recent_capture_events(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 20,
    ) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return {"items": list_capture_events_recent(conn, min(max(limit, 1), 200))}
        finally:
            conn.close()

    @app.get("/capture/stats", dependencies=[Depends(require_local_token)])
    def stats(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return capture_stats(conn)
        finally:
            conn.close()

    return app


def pagination_meta(*, limit: int, offset: int, total: int) -> dict[str, object]:
    next_offset = offset + limit
    previous_offset = max(offset - limit, 0)
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "previous_offset": previous_offset if offset > 0 else None,
    }


def with_media_public_url(item: dict[str, object], settings: Settings) -> dict[str, object]:
    local_path = item.get("media_local_path")
    if isinstance(local_path, str):
        item = dict(item)
        item["media_local_url"] = media_public_url(local_path, settings)
    return item


app = create_app()
