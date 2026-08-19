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
