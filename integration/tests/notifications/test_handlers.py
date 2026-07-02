"""Tests for notification HTTP handlers."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.notifications.handlers import try_handle_get, try_handle_post


def _json_payload(handler: MagicMock):
    raw = handler.wfile.write.call_args.args[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def test_get_notifications_forwards_account_uuid_and_read_type():
    """GET /api/integration/notifications passes account/uuid/readType to downstream."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=18810008888&uuid=abc123&limit=5&read_type=unread"
    )
    upstream_resp = {"code": 200, "msg": "查询成功", "data": []}
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_get(handler, parsed) is True

    mock_post.assert_called_once()
    route_key, body = mock_post.call_args.args
    assert route_key == "get_user_messages"
    assert body["account"] == "18810008888"
    assert body["uuid"] == "abc123"
    assert body["readType"] == "unread"
    assert body["limit"] == 6
    handler.send_response.assert_called_with(200)


def test_get_notifications_default_read_type_all():
    """GET /api/integration/notifications defaults readType to all."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=18810008888&uuid=abc123&limit=5"
    )
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, {"code": 200, "data": []}),
        ) as mock_post:
            assert try_handle_get(handler, parsed) is True

    _, body = mock_post.call_args.args
    assert body["readType"] == "all"


def test_get_notifications_normalizes_kb_messages():
    """GET /api/integration/notifications normalizes downstream data[]."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=18810008888&uuid=abc123"
    )
    upstream_resp = {
        "code": 200,
        "msg": "查询成功",
        "data": [
            {
                "id": 97,
                "massType": 1,
                "massage": "用户 chenyuxin 申请加入您的 学习资料 知识库",
                "showName": "学习资料",
                "targetKbName": "share20",
                "state": 2,
                "createTime": "2026-06-26T14:30:16.017132",
            }
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ):
            assert try_handle_get(handler, parsed) is True

    payload = _json_payload(handler)
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["id"] == "kb:97"
    assert "chenyuxin" in item["title"]
    assert item["source"] == "学习资料"
    assert item["action_status"] == "pending"
    assert item["actionable"] == 1
    assert "actions" not in item


def test_get_notifications_approved_stays_actionable():
    """Approved join requests (massType=1) remain openable with read/delete only."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=18810008888&uuid=abc123"
    )
    upstream_resp = {
        "code": 200,
        "data": [
            {
                "id": 98,
                "massType": 1,
                "massage": "已通过",
                "state": 1,
                "createTime": "2026-06-26T14:30:16.017132",
            }
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ):
            assert try_handle_get(handler, parsed) is True

    item = _json_payload(handler)["items"][0]
    assert item["action_status"] == "approved"
    assert item["actionable"] == 1
    assert "actions" not in item


def test_get_notifications_filters_by_action_status():
    """GET /api/integration/notifications filters by action_status query."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=a&uuid=b&action_status=pending"
    )
    upstream_resp = {
        "code": 200,
        "data": [
            {"id": 1, "massType": 1, "massage": "pending", "state": 2, "createTime": "2026-06-26T14:30:16"},
            {"id": 2, "massType": 1, "massage": "approved", "state": 1, "createTime": "2026-06-26T14:29:16"},
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ):
            assert try_handle_get(handler, parsed) is True

    items = _json_payload(handler)["items"]
    assert len(items) == 1
    assert items[0]["id"] == "kb:1"
    assert items[0]["action_status"] == "pending"


def test_get_notifications_excludes_apply_result_pending():
    """GET /api/integration/notifications drops massType=3 state=2 messages."""
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications?account=wzq&uuid=abc")
    upstream_resp = {
        "code": 200,
        "data": [
            {"id": 110, "massType": 4, "massage": "移出", "state": 2, "createTime": "2026-07-01T14:40:30.235456"},
            {"id": 109, "massType": 3, "massage": "结果待处理", "state": 2, "createTime": "2026-07-01T13:28:11.839666"},
            {"id": 107, "massType": 4, "massage": "移出", "state": 2, "createTime": "2026-07-01T13:01:03.802602"},
            {"id": 102, "massType": 3, "massage": "申请已通过", "state": 1, "createTime": "2026-06-30T18:11:58.647855"},
            {"id": 100, "massType": 3, "massage": "申请已通过", "state": 1, "createTime": "2026-06-30T16:33:06.253961"},
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ):
            assert try_handle_get(handler, parsed) is True

    items = _json_payload(handler)["items"]
    assert len(items) == 4
    ids = {item["id"] for item in items}
    assert "kb:109" not in ids
    assert {"kb:110", "kb:107", "kb:102", "kb:100"} <= ids


def test_get_summary_excludes_apply_result_pending():
    """GET /api/integration/notifications/summary excludes massType=3 state=2."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications/summary?account=wzq&uuid=abc"
    )
    upstream_resp = {
        "code": 200,
        "data": [
            {"id": 109, "massType": 3, "massage": "结果待处理", "state": 2, "createTime": "2026-07-01T13:28:11"},
            {"id": 100, "massType": 3, "massage": "申请已通过", "state": 1, "createTime": "2026-06-30T16:33:06"},
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ):
            assert try_handle_get(handler, parsed) is True

    payload = _json_payload(handler)
    assert payload["total"] == 1
    assert payload["unread"] == 1
    assert payload["by_category"]["kb_apply"]["total"] == 1


def test_get_summary_forwards_account_uuid_to_downstream():
    """GET /api/integration/notifications/summary passes account/uuid/readType unread."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications/summary?account=18810008888&uuid=abc123"
    )
    upstream_resp = {
        "code": 200,
        "msg": "查询成功",
        "data": [
            {
                "id": 97,
                "massType": 1,
                "massage": "新申请",
                "showName": "学习资料",
                "state": 2,
                "createTime": "2026-06-26T14:30:16.017132",
            }
        ],
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_get(handler, parsed) is True

    mock_post.assert_called_once()
    route_key, body = mock_post.call_args.args
    assert route_key == "get_user_messages"
    assert body["account"] == "18810008888"
    assert body["uuid"] == "abc123"
    assert body["readType"] == "unread"

    payload = _json_payload(handler)
    assert payload["total"] == 1
    assert payload["unread"] == 1
    assert "recent_unread" not in payload
    assert body.get("limit") is None


def test_post_read_forwards_kb_ids_to_mark_message_read():
    """POST /read batches kb: IDs into downstream mark_message_read."""
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/read")
    body = {
        "ids": ["kb:123", "kb:456", "local-1"],
        "account": "18810008888",
        "uuid": "abc123",
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, {"code": 200, "msg": "操作成功", "data": None}),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True

    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "mark_message_read"
    assert upstream_body == {"messageId": [123, 456]}

    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["updated"] == 2


def test_post_delete_forwards_kb_ids_to_delete_readed_message():
    """POST /delete batches kb: IDs into downstream delete_readed_message."""
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/delete")
    body = {
        "ids": ["kb:789", "kb:1", "local-1"],
        "account": "18810008888",
        "uuid": "abc123",
    }
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, {"code": 200, "msg": "操作成功", "data": None}),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True

    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "delete_readed_message"
    assert upstream_body == {"messageId": [789, 1]}

    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["deleted"] == 2


def test_post_read_kb_without_account_uuid_skips_downstream():
    """POST /read with kb: IDs but no account/uuid does not call downstream."""
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/read")
    body = {"ids": ["kb:123"]}
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True

    mock_post.assert_not_called()
    payload = _json_payload(handler)
    assert payload["updated"] == 0


def test_post_read_single_id_in_ids_array():
    """POST /read accepts one-element ids array."""
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/read")
    body = {"ids": ["kb:99"], "account": "a", "uuid": "b"}
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            return_value=(200, {"code": 200}),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True

    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "mark_message_read"
    assert upstream_body == {"messageId": [99]}


def test_get_notifications_read_type_all_derives_status_from_seen():
    """GET with read_type=all fetches seen IDs and maps item status."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=a&uuid=b&read_type=all"
    )

    def _mock_post(route_key, body):
        if body.get("readType") == "seen":
            return (200, {"code": 200, "data": [{"id": 97}]})
        return (
            200,
            {
                "code": 200,
                "data": [
                    {
                        "id": 97,
                        "massType": 1,
                        "massage": "read msg",
                        "state": 2,
                        "createTime": "2026-06-26T14:30:16.017132",
                    },
                    {
                        "id": 98,
                        "massType": 4,
                        "massage": "unread msg",
                        "state": 2,
                        "createTime": "2026-06-26T14:29:16.017132",
                    },
                ],
            },
        )

    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=True,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
            side_effect=_mock_post,
        ) as mock_post:
            assert try_handle_get(handler, parsed) is True

    assert mock_post.call_count == 2
    seen_call = mock_post.call_args_list[0]
    assert seen_call.args[1]["readType"] == "seen"
    all_call = mock_post.call_args_list[1]
    assert all_call.args[1]["readType"] == "all"

    items = _json_payload(handler)["items"]
    by_id = {item["id"]: item["status"] for item in items}
    assert by_id["kb:97"] == "read"
    assert by_id["kb:98"] == "unread"


def test_get_notifications_kb_disabled():
    """GET /api/integration/notifications returns empty when KB disabled."""
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=18810008888&uuid=abc123"
    )
    with patch(
        "integration.notifications.handlers.knowledge_base_enabled",
        return_value=False,
    ):
        with patch(
            "integration.notifications.handlers.kb_client.post_json",
        ) as mock_post:
            assert try_handle_get(handler, parsed) is True

    mock_post.assert_not_called()
    payload = _json_payload(handler)
    assert payload["items"] == []
