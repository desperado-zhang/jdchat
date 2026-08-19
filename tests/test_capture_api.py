import sqlite3

from fastapi.testclient import TestClient

from jdchat_gateway.main import create_app
from jdchat_gateway.settings import Settings


def make_event(event_id: str, source: str = "session", msg_id: str = "msg-1") -> dict:
    return {
        "eventId": event_id,
        "source": source,
        "eventType": "message",
        "conversation": {
            "venderId": "shop-1",
            "venderName": "shop",
            "customerApp": "im.customer",
            "customerPin": "customer-pin",
            "sellerApp": "im.waiter",
            "sessionType": "chat",
        },
        "message": {
            "id": msg_id,
            "mid": 100,
            "type": "chat_message",
            "timestamp": 1787111432988,
            "body": {"type": "text", "content": "hello"},
            "from": {"app": "im.customer", "pin": "customer-pin", "clientType": "ios"},
            "to": {"app": "im.waiter", "pin": "waiter-pin"},
        },
        "capturedAt": "2026-08-19T03:50:33+00:00",
    }


def test_capture_events_upserts_duplicate_message_sources(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))

    with TestClient(app) as client:
        response = client.post(
            "/capture/events",
            json={"events": [make_event("evt-1", "session"), make_event("evt-2", "dom")]},
        )
        assert response.status_code == 200
        assert response.json()["inserted"] == 1
        assert response.json()["updated"] == 1
        assert response.json()["duplicates"] == 0

    conn = sqlite3.connect(db_path)
    try:
        messages = conn.execute("SELECT msg_id, source, content FROM messages").fetchall()
        conversations = conn.execute("SELECT conversation_key FROM conversations").fetchall()
        events = conn.execute("SELECT event_id FROM capture_events ORDER BY event_id").fetchall()
    finally:
        conn.close()

    assert len(messages) == 1
    assert messages[0][0] == "msg-1"
    assert messages[0][1] == "session,dom"
    assert messages[0][2] == "hello"
    assert len(conversations) == 1
    assert [row[0] for row in events] == ["evt-1", "evt-2"]


def test_capture_events_counts_exact_duplicate(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))

    with TestClient(app) as client:
        first = client.post("/capture/events", json={"events": [make_event("evt-1")]})
        second = client.post("/capture/events", json={"events": [make_event("evt-1")]})

    assert first.json()["inserted"] == 1
    assert second.json()["duplicates"] == 1


def test_dom_messages_dedupe_when_dom_id_changes(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))
    first = make_event("evt-dom-first", source="dom", msg_id="dom-old-id")
    second = make_event("evt-dom-second", source="dom", msg_id="dom-new-id")
    for event in (first, second):
        event["message"].pop("mid")
        event["message"].pop("timestamp")
        event["message"]["displayTime"] = "06-12 19:31:56"
        event["message"]["direction"] = "seller_or_waiter"
        event["message"]["body"] = {"type": "text", "content": "same dom content"}

    with TestClient(app) as client:
        first_response = client.post("/capture/events", json={"events": [first]})

    conn = sqlite3.connect(db_path)
    try:
        original_dedupe_key = conn.execute("SELECT dedupe_key FROM messages").fetchone()[0]
        conn.execute("UPDATE messages SET dedupe_key = 'legacy-dom-key'")
        conn.execute(
            "UPDATE capture_events SET message_dedupe_key = 'legacy-dom-key' WHERE message_dedupe_key = ?",
            (original_dedupe_key,),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        second_response = client.post("/capture/events", json={"events": [second]})

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["inserted"] == 1
    assert second_response.json()["duplicates"] == 1
    conn = sqlite3.connect(db_path)
    try:
        messages = conn.execute("SELECT dedupe_key, msg_id, content FROM messages").fetchall()
        events = conn.execute(
            "SELECT event_id, message_dedupe_key FROM capture_events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    assert messages == [("legacy-dom-key", "dom-old-id", "same dom content")]
    assert events == [("evt-dom-first", "legacy-dom-key"), ("evt-dom-second", "legacy-dom-key")]


def test_health_initializes_database(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert db_path.exists()


def test_recent_capture_events_returns_page_context_metadata(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))
    event = make_event("evt-history", msg_id="msg-history")
    event["payload"] = {
        "reason": "initial",
        "pageContext": {
            "activeSidebarTab": "history",
            "activeSidebarTabLabel": "历史咨询",
            "historyListVisible": True,
            "historyItemCount": 1,
            "messageNodeCount": 12,
        },
    }

    with TestClient(app) as client:
        capture = client.post("/capture/events", json={"events": [event]})
        recent = client.get("/capture/events/recent")

    assert capture.status_code == 200
    assert recent.status_code == 200
    item = recent.json()["items"][0]
    assert item["event_id"] == "evt-history"
    assert item["source"] == "session"
    assert item["active_sidebar_tab"] == "history"
    assert item["active_sidebar_tab_label"] == "历史咨询"
    assert item["history_list_visible"] is True
    assert item["history_item_count"] == 1
    assert item["message_node_count"] == 12
    assert "content" not in item


def test_viewer_page_is_served(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "jdchat.sqlite3"))

    with TestClient(app) as client:
        response = client.get("/viewer")

    assert response.status_code == 200
    assert "jdchat viewer" in response.text


def test_read_apis_require_token_when_configured(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "jdchat.sqlite3", api_token="local-secret"))

    with TestClient(app) as client:
        unauthorized = client.get("/conversations")
        authorized = client.get("/conversations", headers={"Authorization": "Bearer local-secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_conversation_filters_and_message_order(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))
    first = make_event("evt-first", source="session", msg_id="msg-first")
    first["conversation"]["customerName"] = "Alpha"
    first["message"]["mid"] = 100
    first["message"]["timestamp"] = 1787111431000
    first["message"]["body"]["content"] = "first"

    second = make_event("evt-second", source="dom", msg_id="msg-second")
    second["conversation"]["customerName"] = "Alpha"
    second["message"]["mid"] = 101
    second["message"]["timestamp"] = 1787111433000
    second["message"]["body"]["content"] = "second"

    with TestClient(app) as client:
        capture = client.post("/capture/events", json={"events": [second, first]})
        conversations = client.get("/conversations?q=Alpha&source=dom")
        conversation_key = conversations.json()["items"][0]["conversation_key"]
        messages = client.get(f"/conversations/{conversation_key}/messages?order=asc")

    assert capture.status_code == 200
    assert conversations.status_code == 200
    assert conversations.json()["items"][0]["customer_name"] == "Alpha"
    assert messages.status_code == 200
    assert [item["content"] for item in messages.json()["items"]] == ["first", "second"]
    assert "template_payload" in messages.json()["items"][0]


def test_conversation_list_returns_pagination_metadata(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))
    first = make_event("evt-page-first", msg_id="msg-page-first")
    first["conversation"]["customerName"] = "First"
    second = make_event("evt-page-second", msg_id="msg-page-second")
    second["conversation"]["customerName"] = "Second"
    second["conversation"]["customerPin"] = "second-customer-pin"
    second["message"]["from"]["pin"] = "second-customer-pin"

    with TestClient(app) as client:
        capture = client.post("/capture/events", json={"events": [first, second]})
        first_page = client.get("/conversations?limit=1&offset=0")
        second_page = client.get("/conversations?limit=1&offset=1")

    assert capture.status_code == 200
    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json()["pagination"] == {
        "limit": 1,
        "offset": 0,
        "total": 2,
        "has_more": True,
        "next_offset": 1,
        "previous_offset": None,
    }
    assert second_page.json()["pagination"] == {
        "limit": 1,
        "offset": 1,
        "total": 2,
        "has_more": False,
        "next_offset": None,
        "previous_offset": 0,
    }


def test_session_and_dom_messages_with_same_customer_snapshot_share_conversation(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))
    customer_snapshot = {
        "venderId": "shop-1",
        "venderName": "shop",
        "app": "im.customer",
        "pin": "customer-pin",
        "name": "Alpha",
        "sessionType": "chat",
    }
    customer_event = {
        "eventId": "evt-customer",
        "source": "session",
        "eventType": "message",
        "conversation": customer_snapshot,
        "message": {
            "id": "msg-customer",
            "mid": 100,
            "type": "chat_message",
            "timestamp": 1787111431000,
            "body": {"type": "text", "content": "customer"},
            "from": {"app": "im.customer", "pin": "customer-pin"},
            "to": {"app": "im.waiter", "pin": "waiter-pin"},
        },
        "capturedAt": "2026-08-19T03:50:33+00:00",
    }
    dom_waiter_event = {
        "eventId": "evt-dom-waiter",
        "source": "dom",
        "eventType": "message",
        "conversation": customer_snapshot,
        "message": {
            "id": "dom-waiter",
            "type": "chat_message",
            "direction": "seller_or_waiter",
            "body": {"type": "text", "content": "waiter"},
        },
        "capturedAt": "2026-08-19T03:50:34+00:00",
    }

    with TestClient(app) as client:
        capture = client.post("/capture/events", json={"events": [customer_event, dom_waiter_event]})

    assert capture.status_code == 200
    conn = sqlite3.connect(db_path)
    try:
        conversations = conn.execute("SELECT conversation_key, customer_name FROM conversations").fetchall()
        messages = conn.execute("SELECT direction, source, content FROM messages ORDER BY captured_at").fetchall()
    finally:
        conn.close()

    assert len(conversations) == 1
    assert conversations[0][1] == "Alpha"
    assert messages == [
        ("customer_or_external", "session", "customer"),
        ("seller_or_waiter", "dom", "waiter"),
    ]


def test_image_message_is_cached_to_local_media_dir(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    media_dir = tmp_path / "media"
    app = create_app(Settings(database_path=db_path, media_dir=media_dir))
    event = make_event("evt-image", source="dom", msg_id="msg-image")
    event["message"]["body"] = {
        "type": "image",
        "url": "data:image/png;base64,iVBORw0KGgo=",
        "width": 8,
        "height": 6,
    }

    with TestClient(app) as client:
        capture = client.post("/capture/events", json={"events": [event]})
        conn = sqlite3.connect(db_path)
        try:
            conversation_key = conn.execute("SELECT conversation_key FROM conversations").fetchone()[0]
        finally:
            conn.close()
        messages = client.get(f"/conversations/{conversation_key}/messages?order=asc")

    assert capture.status_code == 200
    assert capture.json()["inserted"] == 1
    item = messages.json()["items"][0]
    assert item["body_type"] == "image"
    assert item["media_download_status"] == "saved"
    assert item["media_mime_type"] == "image/png"
    assert item["media_local_path"].endswith(".png")
    assert item["media_local_url"].startswith("/media/")
    assert (media_dir / item["media_local_path"]).read_bytes() == b"\x89PNG\r\n\x1a\n"
