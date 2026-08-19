from jdchat_gateway.dedupe import compute_dedupe_key


def test_msg_id_has_highest_priority() -> None:
    left = compute_dedupe_key(
        platform="jd_dongdong",
        conversation_key="conv-a",
        msg_id="message-1",
        mid=1,
        timestamp=100,
        direction="customer_or_external",
        body_type="text",
        content_hash_value="aaa",
    )
    right = compute_dedupe_key(
        platform="jd_dongdong",
        conversation_key="conv-b",
        msg_id="message-1",
        mid=2,
        timestamp=200,
        direction="seller_or_waiter",
        body_type="image",
        content_hash_value="bbb",
    )

    assert left == right


def test_mid_uses_conversation_scope_when_msg_id_is_missing() -> None:
    left = compute_dedupe_key(platform="jd_dongdong", conversation_key="conv-a", mid=1)
    right = compute_dedupe_key(platform="jd_dongdong", conversation_key="conv-b", mid=1)

    assert left != right


def test_fallback_changes_with_content_hash() -> None:
    left = compute_dedupe_key(
        platform="jd_dongdong",
        conversation_key="conv-a",
        timestamp=100,
        direction="customer_or_external",
        body_type="text",
        content_hash_value="aaa",
    )
    right = compute_dedupe_key(
        platform="jd_dongdong",
        conversation_key="conv-a",
        timestamp=100,
        direction="customer_or_external",
        body_type="text",
        content_hash_value="bbb",
    )

    assert left != right
