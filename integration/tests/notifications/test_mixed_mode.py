"""Tests for mixed-mode notifications: upstream skill_publish + KB downstream.

Both sources are fetched live at query time and merged; read state is owned
by the respective service (nothing persists in the local notifications table).
"""

import json
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.notifications import handlers as notif_handlers
from integration.notifications.constants import NotificationCategory


def _json_payload(handler: MagicMock):
    raw = handler.wfile.write.call_args.args[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _upstream_resp(items):
    return {"code": 200, "data": items}


def _kb_item(kb_id, minutes_offset=0):
    return {
        "id": kb_id,
        "massType": 1,
        "massage": f"kb {kb_id}",
        "state": 2,
        "createTime": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - minutes_offset * 60)
        ),
    }


def _sp_event(event_id, scene="1", result="1", name="demo-skill",
              minutes_offset=0):
    return {
        "id": event_id,
        "scene": scene,
        "result": result,
        "applicationId": f"up-{event_id}",
        "name": name,
        "version": "1.0.0",
        "auditComment": "",
        "createTime": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - minutes_offset * 60)
        ),
    }


def _patch_sp(events_by_read_type, mark_read=None):
    """Patch the skill_publish client calls used by the notifications API.

    ``events_by_read_type`` maps read_type ("all"/"unread"/"seen") -> event list.
    """

    def fake_fetch(**kwargs):
        return events_by_read_type.get(kwargs.get("read_type", "all"), [])

    fetch_patch = patch(
        "integration.skill_publish.client.fetch_external_notifications",
        side_effect=fake_fetch,
    )
    mark_patch = patch(
        "integration.skill_publish.client.mark_external_notifications_read",
        side_effect=mark_read or (lambda **kwargs: True),
    )
    return fetch_patch, mark_patch


_SYNC = patch(
    "integration.skill_publish.sync.sync_notifications_for_account",
    return_value=0,
)


def test_list_merges_upstream_events_and_kb_sorted_desc():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications?account=acct-a&uuid=u")
    fetch_patch, mark_patch = _patch_sp({
        "all": [_sp_event(11, minutes_offset=1)],
        "seen": [],
    })
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, _upstream_resp([_kb_item(97, minutes_offset=30)])),
            ), _SYNC as mock_sync, fetch_patch, mark_patch:
                assert notif_handlers.try_handle_get(handler, parsed) is True

    mock_sync.assert_called_once_with("acct-a", external_user_id="u")
    payload = _json_payload(handler)
    ids = [item["id"] for item in payload["items"]]
    assert ids == ["skill_publish:11", "kb:97"]
    sp_item = payload["items"][0]
    assert sp_item["category"] == NotificationCategory.SKILL_PUBLISH
    assert sp_item["title"] == "技能《demo-skill》审核通过"
    assert sp_item["action_status"] == "approved"
    assert sp_item["actionable"] == 1
    assert sp_item["status"] == "unread"  # not in seen set


def test_list_read_type_all_marks_seen_events_read():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications?account=acct-a&uuid=u")
    fetch_patch, mark_patch = _patch_sp({
        "all": [_sp_event(11), _sp_event(12)],
        "seen": [_sp_event(12)],
    })
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, _upstream_resp([])),
            ), fetch_patch, mark_patch:
                assert notif_handlers.try_handle_get(handler, parsed) is True

    by_id = {i["id"]: i["status"] for i in _json_payload(handler)["items"]}
    assert by_id["skill_publish:11"] == "unread"
    assert by_id["skill_publish:12"] == "read"


def test_list_read_type_unread_skips_seen_fetch():
    handler = MagicMock()
    parsed = urlparse(
        "/api/integration/notifications?account=acct-a&uuid=u&read_type=unread"
    )
    fetch_patch, mark_patch = _patch_sp({"unread": [_sp_event(11, result="0")]})
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, _upstream_resp([])),
            ), _SYNC, fetch_patch as mock_fetch, mark_patch:
                assert notif_handlers.try_handle_get(handler, parsed) is True

    read_types = [c.kwargs.get("read_type") for c in mock_fetch.call_args_list]
    assert read_types == ["unread"]
    item = _json_payload(handler)["items"][0]
    assert item["status"] == "unread"
    assert item["action_status"] == "rejected"


def test_list_admin_offline_event_shape():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications?account=acct-a&uuid=u")
    fetch_patch, mark_patch = _patch_sp({"all": [_sp_event(15, scene="3")]})
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, _upstream_resp([])),
            ), _SYNC, fetch_patch, mark_patch:
                assert notif_handlers.try_handle_get(handler, parsed) is True

    item = _json_payload(handler)["items"][0]
    assert item["title"] == "技能《demo-skill》已被管理端下架"
    assert item["actionable"] == 0
    assert item["action_status"] == ""


def test_list_upstream_failure_still_returns_kb():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications?account=acct-a&uuid=u")
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch(
                "integration.skill_publish.client.fetch_external_notifications",
                side_effect=RuntimeError("boom"),
            ), _SYNC:
                with patch.object(
                    notif_handlers.kb_client, "post_json",
                    return_value=(200, _upstream_resp([_kb_item(97)])),
                ):
                    assert notif_handlers.try_handle_get(handler, parsed) is True

    payload = _json_payload(handler)
    assert [item["id"] for item in payload["items"]] == ["kb:97"]


def test_summary_merges_categories():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/summary?account=acct-a&uuid=u")
    fetch_patch, mark_patch = _patch_sp({"unread": [_sp_event(11), _sp_event(12)]})
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, _upstream_resp([_kb_item(97)])),
            ), _SYNC, fetch_patch, mark_patch:
                assert notif_handlers.try_handle_get(handler, parsed) is True

    payload = _json_payload(handler)
    assert payload["total"] == 3
    assert payload["unread"] == 3
    assert payload["by_category"][NotificationCategory.SKILL_PUBLISH] == {
        "total": 2, "unread": 2,
    }
    assert payload["by_category"][NotificationCategory.KB_APPLY]["total"] == 1
    assert "recent_unread" not in payload


def test_post_read_routes_by_prefix():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/read")
    body = {
        "ids": ["kb:123", "skill_publish:11"],
        "account": "acct-a",
        "uuid": "u",
    }
    fetch_patch, mark_patch = _patch_sp(
        {}, mark_read=lambda **kwargs: True,
    )
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, {"code": 200, "data": None}),
            ), fetch_patch, mark_patch as mock_mark:
                assert notif_handlers.try_handle_post(handler, parsed, body) is True

    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["updated"] == 2
    kwargs = mock_mark.call_args.kwargs
    assert kwargs["external_user_id"] == "u"
    assert kwargs["event_ids"] == [11]


def test_post_read_upstream_failure_counts_zero():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/read")
    body = {"ids": ["skill_publish:11"], "account": "acct-a"}
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch(
                "integration.skill_publish.client.mark_external_notifications_read",
                side_effect=RuntimeError("boom"),
            ):
                assert notif_handlers.try_handle_post(handler, parsed, body) is True

    assert _json_payload(handler)["updated"] == 0


def test_post_delete_ignores_skill_publish_ids():
    handler = MagicMock()
    parsed = urlparse("/api/integration/notifications/delete")
    body = {
        "ids": ["kb:123", "skill_publish:11"],
        "account": "acct-a",
        "uuid": "u",
    }
    with patch.object(notif_handlers, "knowledge_base_enabled", return_value=True):
        with patch.object(notif_handlers, "skill_publish_enabled", return_value=True):
            with patch.object(
                notif_handlers.kb_client, "post_json",
                return_value=(200, {"code": 200, "data": None}),
            ) as mock_post:
                assert notif_handlers.try_handle_post(handler, parsed, body) is True

    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "delete_readed_message"
    assert upstream_body == {"messageId": [123]}
    assert _json_payload(handler)["deleted"] == 1
