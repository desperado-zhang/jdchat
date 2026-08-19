import json

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
