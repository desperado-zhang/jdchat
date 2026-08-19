from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from jdchat_gateway import __version__
from jdchat_gateway.db import connect, init_db
from jdchat_gateway.models import CaptureBatchIn, CaptureRejected, CaptureResponse, HealthResponse
from jdchat_gateway.normalize import normalize_capture_event
from jdchat_gateway.repositories import (
    list_conversations,
    list_messages,
    record_capture_event,
    upsert_conversation,
    upsert_message,
)
from jdchat_gateway.settings import Settings


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

    @app.get("/conversations")
    def conversations(
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
    ) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return {"items": list_conversations(conn, min(max(limit, 1), 200))}
        finally:
            conn.close()

    @app.get("/conversations/{conversation_key}/messages")
    def conversation_messages(
        conversation_key: str,
        settings: Annotated[Settings, Depends(get_settings)],
        limit: int = 50,
    ) -> dict[str, object]:
        conn = connect(settings.database_path)
        try:
            return {"items": list_messages(conn, conversation_key, min(max(limit, 1), 500))}
        finally:
            conn.close()

    return app


app = create_app()
