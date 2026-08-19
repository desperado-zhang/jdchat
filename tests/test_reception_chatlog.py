import json
import sqlite3

from fastapi.testclient import TestClient

from jdchat_gateway.dedupe import hash_identifier, sha256_text
from jdchat_gateway.main import create_app
from jdchat_gateway.reception import normalize_reception_chatlog_event
from jdchat_gateway.settings import Settings


def make_reception_event(event_id: str, mid: str = "9001") -> dict:
    return {
        "eventId": event_id,
        "source": "reception_chatlog",
        "eventType": "message",
        "conversation": {
            "cidHash": "cid-hash-1",
            "customerHash": hash_identifier("customer-pin"),
            "waiterHash": hash_identifier("waiter-pin"),
            "mallName": "shop",
            "groupName": "售前",
            "sessionTypeDesc": "在线咨询",
        },
        "message": {
            "mid": mid,
            "created": "2026-08-19 21:12:33",
            "waiterSend": False,
            "type": "text",
            "content": "您好",
        },
        "payload": {
            "networkContext": {
                "url": "https://kf.jd.com/waiterSession/queryChatLog?cid=<redacted>",
            }
        },
        "capturedAt": "2026-08-19T13:12:34+00:00",
    }


def test_reception_chatlog_normalize_is_independent_from_dongdong_normalizer() -> None:
    normalized = normalize_reception_chatlog_event(
        {
            "eventId": "reception-text-1",
            "source": "reception_chatlog",
            "eventType": "message",
            "conversation": {
                "cidHash": "cid-hash-1",
                "customer": "customer-pin",
                "service": "waiter-pin",
                "mallName": "shop",
                "sessionTypeDesc": "在线咨询",
            },
            "message": {
                "mid": "9001",
                "uuid": "uuid-1",
                "sid": "sid-1",
                "created": "2026-08-19 21:12:33",
                "waiterSend": False,
                "type": "text",
                "content": "您好",
                "customer": "customer-pin",
                "waiter": "waiter-pin",
            },
        }
    )

    session = normalized["session"]
    message = normalized["message"]
    assert session["platform"] == "jd_jingmai_reception"
    assert session["conversation_key"] == sha256_text("jd_jingmai_reception:cid-hash-1")
    assert session["customer_hash"] == hash_identifier("customer-pin")
    assert session["waiter_hash"] == hash_identifier("waiter-pin")
    assert message["msg_id"] == "jm:cid-hash-1:9001"
    assert message["mid"] == "9001"
    assert message["local_id"] == hash_identifier("sid-1")
    assert message["direction"] == "customer_or_external"
    assert message["body_type"] == "text"
    assert message["message_at"] == "2026-08-19T13:12:33+00:00"

    raw_message = json.loads(message["raw_json"])
    raw_session = json.loads(session["raw_session"])
    assert raw_message["customer"]["redacted"] is True
    assert raw_message["waiter"]["redacted"] is True
    assert raw_message["uuid"]["redacted"] is True
    assert raw_message["sid"]["redacted"] is True
    assert raw_session["customer"]["redacted"] is True
    assert raw_session["service"]["redacted"] is True


def test_reception_chatlog_image_message_reads_img_url_and_waiter_direction() -> None:
    normalized = normalize_reception_chatlog_event(
        {
            "eventId": "reception-image-1",
            "source": "reception_chatlog",
            "eventType": "message",
            "conversation": {
                "cidHash": "cid-hash-2",
                "customerHash": hash_identifier("customer-pin"),
                "waiterHash": hash_identifier("waiter-pin"),
            },
            "message": {
                "mid": "9002",
                "created": "2026-08-19 21:14:01",
                "waiterSend": True,
                "type": "image",
                "content": "",
                "imgUrl": "https://img.example.test/a.png",
                "width": "640",
                "height": "480",
            },
        }
    )

    message = normalized["message"]
    assert message["direction"] == "seller_or_waiter"
    assert message["body_type"] == "image"
    assert message["media_url"] == "https://img.example.test/a.png"
    assert message["media_width"] == 640
    assert message["media_height"] == 480


def test_reception_endpoint_dedupes_into_isolated_tables(tmp_path) -> None:
    db_path = tmp_path / "jdchat.sqlite3"
    app = create_app(Settings(database_path=db_path))

    with TestClient(app) as client:
        response = client.post(
            "/reception/chatlog/events",
            json={"events": [make_reception_event("reception-evt-1"), make_reception_event("reception-evt-2")]},
        )
        stats = client.get("/reception/chatlog/stats")
        sessions_response = client.get("/reception/chatlog/sessions")

    assert response.status_code == 200
    assert response.json()["accepted"] == 2
    assert response.json()["inserted"] == 1
    assert response.json()["duplicates"] == 1
    assert stats.status_code == 200
    assert stats.json()["totals"] == {"sessions": 1, "messages": 1, "events": 2}
    assert sessions_response.status_code == 200
    assert sessions_response.json()["items"][0]["message_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        old_counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM conversations),
              (SELECT COUNT(*) FROM messages),
              (SELECT COUNT(*) FROM capture_events)
            """
        ).fetchone()
        new_messages = conn.execute(
            """
            SELECT msg_id, mid, source, direction, message_at
            FROM reception_chatlog_messages
            """
        ).fetchall()
        new_events = conn.execute(
            "SELECT event_id, source FROM reception_chatlog_events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()

    assert old_counts == (0, 0, 0)
    assert new_messages == [
        (
            "jm:cid-hash-1:9001",
            "9001",
            "reception_chatlog",
            "customer_or_external",
            "2026-08-19T13:12:33+00:00",
        )
    ]
    assert new_events == [
        ("reception-evt-1", "reception_chatlog"),
        ("reception-evt-2", "reception_chatlog"),
    ]


def test_legacy_capture_endpoint_rejects_reception_source(tmp_path) -> None:
    app = create_app(Settings(database_path=tmp_path / "jdchat.sqlite3"))

    with TestClient(app) as client:
        response = client.post("/capture/events", json={"events": [make_reception_event("wrong-endpoint")]})

    assert response.status_code == 422
