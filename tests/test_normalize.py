import json

from jdchat_gateway.dedupe import hash_identifier, sha256_text
from jdchat_gateway.normalize import normalize_capture_event


def test_normalize_text_message_and_direction() -> None:
    normalized = normalize_capture_event(
        {
            "eventId": "evt-1",
            "source": "session",
            "eventType": "message",
            "conversation": {
                "venderId": "shop-1",
                "customerApp": "im.customer",
                "customerPin": "customer-pin",
                "sellerApp": "im.waiter",
                "sessionType": "chat",
            },
            "message": {
                "id": "msg-1",
                "mid": 10,
                "type": "chat_message",
                "timestamp": 1787111432988,
                "body": {"type": "text", "content": "hello"},
                "from": {"app": "im.customer", "pin": "customer-pin", "clientType": "ios"},
                "to": {"app": "im.waiter", "pin": "waiter-pin"},
            },
        }
    )

    message = normalized["message"]
    assert normalized["conversation"]["vender_id"] == "shop-1"
    assert normalized["conversation"]["customer_pin_hash"]
    assert message["direction"] == "customer_or_external"
    assert message["body_type"] == "text"
    assert message["content"] == "hello"
    assert message["content_hash"]
    assert message["message_at"].startswith("2026-08-19T03:50:32")


def test_raw_json_redacts_sensitive_values() -> None:
    normalized = normalize_capture_event(
        {
            "eventId": "evt-2",
            "source": "xhr",
            "eventType": "message",
            "conversation": {"customerApp": "im.customer", "customerPin": "customer-pin"},
            "message": {
                "id": "msg-2",
                "body": {"type": "text", "content": "hello"},
                "from": {"app": "im.customer", "pin": "customer-pin"},
                "to": {"app": "im.waiter", "pin": "waiter-pin"},
                "access_token": "secret-token",
            },
        }
    )

    raw = json.loads(normalized["message"]["raw_json"])
    assert raw["access_token"]["redacted"] is True
    assert raw["from"]["pin"]["redacted"] is True


def test_session_customer_snapshot_unifies_customer_and_waiter_messages() -> None:
    customer_snapshot = {
        "venderId": "shop-1",
        "venderName": "shop",
        "app": "im.customer",
        "pin": "customer-pin",
        "name": "Alpha",
        "sessionType": "chat",
    }
    customer_message = normalize_capture_event(
        {
            "eventId": "evt-customer",
            "source": "session",
            "eventType": "message",
            "conversation": customer_snapshot,
            "message": {
                "id": "msg-customer",
                "mid": 10,
                "type": "chat_message",
                "timestamp": 1787111432988,
                "body": {"type": "text", "content": "customer says hello"},
                "from": {"app": "im.customer", "pin": "customer-pin"},
                "to": {"app": "im.waiter", "pin": "waiter-pin"},
            },
        }
    )
    waiter_message = normalize_capture_event(
        {
            "eventId": "evt-waiter",
            "source": "session",
            "eventType": "message",
            "conversation": customer_snapshot,
            "message": {
                "id": "msg-waiter",
                "mid": 11,
                "type": "chat_message",
                "timestamp": 1787111433988,
                "body": {"type": "text", "content": "waiter replies"},
                "from": {"app": "im.waiter", "pin": "waiter-pin"},
                "to": {"app": "im.customer", "pin": "customer-pin"},
            },
        }
    )

    assert customer_message["conversation"]["conversation_key"] == waiter_message["conversation"]["conversation_key"]
    assert customer_message["conversation"]["customer_app"] == "im.customer"
    assert customer_message["conversation"]["customer_name"] == "Alpha"
    assert customer_message["message"]["direction"] == "customer_or_external"
    assert waiter_message["message"]["direction"] == "seller_or_waiter"


def test_reception_chatlog_text_message_uses_reception_platform_and_local_time() -> None:
    normalized = normalize_capture_event(
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

    conversation = normalized["conversation"]
    message = normalized["message"]
    assert conversation["platform"] == "jd_jingmai_reception"
    assert conversation["conversation_key"] == sha256_text("jd_jingmai_reception:cid-hash-1")
    assert conversation["customer_app"] == "jingmai.customer"
    assert conversation["customer_pin_hash"] == hash_identifier("customer-pin")
    assert conversation["seller_pin_hash"] == hash_identifier("waiter-pin")
    assert message["platform"] == "jd_jingmai_reception"
    assert message["msg_id"] == "jm:cid-hash-1:9001"
    assert message["mid"] == 9001
    assert message["local_id"] == "sid-1"
    assert message["direction"] == "customer_or_external"
    assert message["body_type"] == "text"
    assert message["message_at"] == "2026-08-19T13:12:33+00:00"

    raw_message = json.loads(message["raw_json"])
    raw_conversation = json.loads(conversation["raw_customer"])
    assert raw_message["customer"]["redacted"] is True
    assert raw_message["waiter"]["redacted"] is True
    assert raw_message["uuid"]["redacted"] is True
    assert raw_conversation["customer"]["redacted"] is True
    assert raw_conversation["service"]["redacted"] is True


def test_reception_chatlog_image_message_reads_img_url_and_waiter_direction() -> None:
    normalized = normalize_capture_event(
        {
            "eventId": "reception-image-1",
            "source": "reception_chatlog",
            "eventType": "message",
            "conversation": {
                "cidHash": "cid-hash-2",
                "customer": "customer-pin",
                "service": "waiter-pin",
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
                "customer": "customer-pin",
                "waiter": "waiter-pin",
            },
        }
    )

    message = normalized["message"]
    assert message["direction"] == "seller_or_waiter"
    assert message["body_type"] == "image"
    assert message["media_url"] == "https://img.example.test/a.png"
    assert message["media_width"] == 640
    assert message["media_height"] == 480
    assert message["from_app"] == "jingmai.waiter"
    assert message["to_app"] == "jingmai.customer"
