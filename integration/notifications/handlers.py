"""HTTP handlers for notifications API (knowledge base only)."""

import traceback
from urllib.parse import parse_qs

from api.helpers import j

from integration.config import knowledge_base_enabled
from integration.knowledge_base import client as kb_client
from integration.notifications.constants import NotificationCategory, VALID_READ_TYPES
from integration.notifications.filters import (
    exclude_apply_result_pending,
    filter_by_action_status,
)
from integration.notifications.normalize import (
    KB_ID_PREFIX,
    normalize_kb_message,
)

DEFAULT_READ_TYPE = "all"
SUMMARY_READ_TYPE = "unread"


def _extract_kb_data(response) -> list:
    """Extract message list from downstream {code, msg, data} envelope."""
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return data
    return []


def _parse_read_type(raw: str | None, *, default: str) -> str:
    if raw and raw in VALID_READ_TYPES:
        return raw
    return default


def _extract_kb_ids(ids: list[str]) -> list[str]:
    """Strip kb: prefix from notification IDs."""
    return [i[len(KB_ID_PREFIX):] for i in ids if i.startswith(KB_ID_PREFIX)]


def _fetch_seen_message_ids(*, account, uuid) -> set[str]:
    """Fetch downstream seen message IDs for read_type=all status derivation."""
    if not knowledge_base_enabled() or not account or not uuid:
        return set()
    try:
        _, response = kb_client.post_json("get_user_messages", {
            "account": account,
            "uuid": uuid,
            "readType": "seen",
        })
        messages = _extract_kb_data(response)
        return {
            str(msg.get("id", "") or "")
            for msg in messages
            if msg.get("id") is not None
        }
    except Exception as e:
        print(
            f"[webui] notifications: failed to fetch seen message ids: {e}\n{traceback.format_exc()}",
            flush=True,
        )
        return set()


def _fetch_kb_messages(*, account, uuid, read_type, limit, cursor=None):
    """Fetch messages from downstream knowledge base service."""
    if not knowledge_base_enabled():
        return []

    try:
        seen_ids = None
        if read_type == "all":
            seen_ids = _fetch_seen_message_ids(account=account, uuid=uuid)

        body: dict = {
            "account": account,
            "uuid": uuid,
            "readType": read_type,
            "limit": limit,
        }
        if cursor is not None:
            body["before"] = cursor
        _, response = kb_client.post_json("get_user_messages", body)
        messages = _extract_kb_data(response)
        return [
            normalize_kb_message(msg, read_type=read_type, seen_ids=seen_ids)
            for msg in messages
        ]
    except Exception as e:
        print(
            f"[webui] notifications: failed to fetch kb messages: {e}\n{traceback.format_exc()}",
            flush=True,
        )
        return []


def _paginate_items(items, limit, cursor):
    """Sort by created_at DESC and apply cursor pagination."""
    items.sort(key=lambda x: x["created_at"], reverse=True)

    if cursor is not None:
        items = [item for item in items if item["created_at"] < cursor]

    result = items[: limit + 1]
    if len(result) > limit:
        next_cursor = result[limit]["created_at"]
        result = result[:limit]
    else:
        next_cursor = None
    return result, next_cursor


def _fetch_kb_summary(*, account, uuid, read_type):
    """Build summary from downstream get_user_messages (counts only)."""
    empty = {
        "total": 0,
        "unread": 0,
        "by_category": {},
    }
    if not knowledge_base_enabled():
        return empty

    try:
        _, response = kb_client.post_json("get_user_messages", {
            "account": account,
            "uuid": uuid,
            "readType": read_type,
        })
        kb_messages = _extract_kb_data(response)
        items = exclude_apply_result_pending([
            normalize_kb_message(msg, read_type=read_type)
            for msg in kb_messages
        ])
        kb_count = len(items)
        return {
            "total": kb_count,
            "unread": kb_count,
            "by_category": {
                NotificationCategory.KB_APPLY: {
                    "total": kb_count,
                    "unread": kb_count,
                }
            },
        }
    except Exception as e:
        print(
            f"[webui] notifications: failed to fetch kb summary: {e}\n{traceback.format_exc()}",
            flush=True,
        )
        return empty


def _parse_kb_message_ids(kb_ids: list[str]) -> list[int]:
    message_ids: list[int] = []
    for kb_id in kb_ids:
        try:
            message_ids.append(int(kb_id))
        except ValueError:
            continue
    return message_ids


def _mark_kb_messages_read(kb_ids: list[str]) -> int:
    """Forward kb message IDs to downstream mark_message_read."""
    if not kb_ids or not knowledge_base_enabled():
        return 0
    message_ids = _parse_kb_message_ids(kb_ids)
    if not message_ids:
        return 0
    try:
        _, resp = kb_client.post_json("mark_message_read", {"messageId": message_ids})
        if isinstance(resp, dict) and resp.get("code") == 200:
            return len(message_ids)
    except Exception as e:
        print(
            f"[webui] notifications: kb read forward failed: {e}\n{traceback.format_exc()}",
            flush=True,
        )
    return 0


def _delete_kb_messages(kb_ids: list[str]) -> int:
    """Forward kb message IDs to downstream delete_readed_message."""
    if not kb_ids or not knowledge_base_enabled():
        return 0
    message_ids = _parse_kb_message_ids(kb_ids)
    if not message_ids:
        return 0
    try:
        _, resp = kb_client.post_json("delete_readed_message", {"messageId": message_ids})
        if isinstance(resp, dict) and resp.get("code") == 200:
            return len(message_ids)
    except Exception as e:
        print(
            f"[webui] notifications: kb delete forward failed: {e}\n{traceback.format_exc()}",
            flush=True,
        )
    return 0


def try_handle_get(handler, parsed) -> bool:
    """Handle GET requests for notifications."""
    path = parsed.path

    if path == "/api/integration/notifications":
        params = parse_qs(parsed.query)

        limit_str = params.get("limit", ["20"])[0]
        try:
            limit = int(limit_str)
        except ValueError:
            limit = 20

        cursor_str = params.get("cursor", [None])[0]
        cursor = None
        if cursor_str:
            try:
                cursor = float(cursor_str)
            except ValueError:
                cursor = None

        account = (params.get("account", [None])[0] or "").strip()
        uuid = (params.get("uuid", [None])[0] or "").strip()
        read_type = _parse_read_type(
            params.get("read_type", [None])[0],
            default=DEFAULT_READ_TYPE,
        )
        action_status_filter = (params.get("action_status", [None])[0] or "").strip() or None

        items = _fetch_kb_messages(
            account=account,
            uuid=uuid,
            read_type=read_type,
            limit=limit + 1,
            cursor=cursor,
        )
        items = exclude_apply_result_pending(items)
        items = filter_by_action_status(items, action_status_filter)
        items, next_cursor = _paginate_items(items, limit, cursor)

        j(handler, {
            "items": items,
            "next_cursor": next_cursor,
        })
        return True

    if path == "/api/integration/notifications/summary":
        params = parse_qs(parsed.query)

        account = (params.get("account", [None])[0] or "").strip()
        uuid = (params.get("uuid", [None])[0] or "").strip()
        read_type = _parse_read_type(
            params.get("read_type", [None])[0],
            default=SUMMARY_READ_TYPE,
        )

        j(handler, _fetch_kb_summary(
            account=account,
            uuid=uuid,
            read_type=read_type,
        ))
        return True

    return False


def try_handle_post(handler, parsed, body) -> bool:
    """Handle POST requests for notifications."""
    path = parsed.path

    if path == "/api/integration/notifications/read":
        raw_ids = body.get("ids", [])
        ids = raw_ids if isinstance(raw_ids, list) else []

        kb_ids = _extract_kb_ids(ids)
        account = str(body.get("account", "") or "").strip()
        uuid = str(body.get("uuid", "") or "").strip()

        updated = _mark_kb_messages_read(kb_ids) if kb_ids and account and uuid else 0

        j(handler, {"ok": True, "updated": updated})
        return True

    if path == "/api/integration/notifications/delete":
        raw_ids = body.get("ids", [])
        ids = raw_ids if isinstance(raw_ids, list) else []

        kb_ids = _extract_kb_ids(ids)
        account = str(body.get("account", "") or "").strip()
        uuid = str(body.get("uuid", "") or "").strip()

        deleted = _delete_kb_messages(kb_ids) if kb_ids and account and uuid else 0

        j(handler, {"ok": True, "deleted": deleted})
        return True

    return False
