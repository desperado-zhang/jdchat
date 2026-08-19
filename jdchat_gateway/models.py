from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CaptureSource = Literal[
    "websocket",
    "xhr",
    "fetch",
    "session",
    "dom",
    "manual_scroll",
    "reception_list",
    "reception_chatlog",
    "reception_dom",
]


class CaptureEventIn(BaseModel):
    event_id: str | None = Field(default=None, alias="eventId")
    source: CaptureSource
    event_type: str = Field(default="message", alias="eventType")
    conversation: dict[str, Any] | None = None
    message: dict[str, Any] | None = None
    payload: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    captured_at: str | None = Field(default=None, alias="capturedAt")

    model_config = {"populate_by_name": True, "extra": "allow"}


class CaptureBatchIn(BaseModel):
    plugin_instance_id: str | None = Field(default=None, alias="pluginInstanceId")
    waiter_account_hash: str | None = Field(default=None, alias="waiterAccountHash")
    shop_id: str | None = Field(default=None, alias="shopId")
    events: list[CaptureEventIn]

    model_config = {"populate_by_name": True, "extra": "allow"}


class CaptureRejected(BaseModel):
    event_id: str | None = Field(default=None, alias="eventId")
    reason: str

    model_config = {"populate_by_name": True}


class CaptureResponse(BaseModel):
    accepted: int
    inserted: int
    updated: int
    duplicates: int
    rejected: list[CaptureRejected]


class HealthResponse(BaseModel):
    ok: bool
    database: str
    version: str
