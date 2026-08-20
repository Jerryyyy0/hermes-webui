"""HTTP handlers for notifications API.

Mixed mode: ``kb_apply`` items come from the downstream KB service and
``skill_publish`` items from the upstream SkillHub event stream - both fetched
live at query time, normalized in memory and merged (created_at DESC, cursor
pagination). Nothing is persisted in the local notifications table; read
state is owned by the respective downstream/upstream service.
"""

import logging
from urllib.parse import parse_qs

from api.helpers import j

from integration.config import knowledge_base_enabled, skill_publish_enabled
from integration.knowledge_base import client as kb_client
from integration.notifications.constants import NotificationCategory, VALID_READ_TYPES
from integration.notifications.filters import (
    exclude_apply_result_pending,
    filter_by_action_status,
)
from integration.project_logging import get_logger
from integration.notifications.normalize import (
    KB_ID_PREFIX,
    normalize_kb_message,
    normalize_skill_publish_event,
)

logger = get_logger(__name__)

DEFAULT_READ_TYPE = "all"
SUMMARY_READ_TYPE = "unread"


def _extract_kb_ids(ids: list[str]) -> list[str]:
    """Strip kb: prefix from notification IDs."""
    return [i[len(KB_ID_PREFIX):] for i in ids if i.startswith(KB_ID_PREFIX)]


def _extract_skill_publish_ids(ids: list[str]) -> list[str]:
    """Strip skill_publish: prefix, keeping the upstream event IDs."""
    prefix = "skill_publish:"
    return [i[len(prefix):] for i in ids if i.startswith(prefix)]


def _sync_skill_publish(account: str, uuid: str = "") -> None:
    """Apply upstream audit events to local application state (best effort)."""
    if not skill_publish_enabled() or not account:
        return
    try:
        from integration.skill_publish.sync import sync_notifications_for_account

        sync_notifications_for_account(account, external_user_id=uuid)
    except Exception as exc:
        logger.exception(
            "notifications: skill_publish sync failed account=%s error=%s",
            account, exc,
        )


def _fetch_skill_publish_events(
    *, account: str, uuid: str = "", read_type: str, limit: int
) -> list[dict]:
    """Pull the upstream event stream for display (live, never persisted)."""
    if not skill_publish_enabled() or not account:
        return []
    ext_id = uuid or account
    try:
        from integration.config import skill_publish_platform
        from integration.skill_publish.client import (
            fetch_external_notifications,
        )

        seen_ids = None
        if read_type == "all":
            seen_events = fetch_external_notifications(
                platform=skill_publish_platform(),
                external_user_id=ext_id,
                limit=limit,
                read_type="seen",
            )
            seen_ids = {
                str(e.get("id", "") or "")
                for e in seen_events
                if e.get("id") is not None
            }
        events = fetch_external_notifications(
            platform=skill_publish_platform(),
            external_user_id=ext_id,
            limit=limit,
            read_type=read_type,
        )
        return [
            normalize_skill_publish_event(
                e, read_type=read_type, seen_ids=seen_ids
            )
            for e in events
        ]
    except Exception as exc:
        logger.exception(
            "notifications: skill_publish fetch failed account=%s error=%s",
            account, exc,
        )
        return []


def _fetch_skill_publish_summary(
    *, account: str, uuid: str = "", read_type: str
) -> dict:
    """Count skill_publish events from upstream (unread by default)."""
    empty = {"total": 0, "unread": 0, "by_category": {}}
    if not skill_publish_enabled() or not account:
        return empty
    ext_id = uuid or account
    try:
        from integration.config import skill_publish_platform
        from integration.skill_publish.client import (
            fetch_external_notifications,
        )

        events = fetch_external_notifications(
            platform=skill_publish_platform(),
            external_user_id=ext_id,
            read_type=read_type,
        )
        count = len(events)
        return {
            "total": count,
            "unread": count,
            "by_category": {
                NotificationCategory.SKILL_PUBLISH: {
                    "total": count,
                    "unread": count,
                }
            },
        }
    except Exception as exc:
        logger.exception(
            "notifications: skill_publish summary failed account=%s error=%s",
            account, exc,
        )
        return empty


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
    except Exception as exc:
        logger.exception("notifications: failed to fetch seen message ids error=%s", exc)
        return set()


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
    except Exception as exc:
        logger.exception(
            "notifications: failed to fetch kb messages operation=get_user_messages read_type=%s error=%s",
            read_type, exc,
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
    except Exception as exc:
        logger.exception(
            "notifications: failed to fetch kb summary operation=get_user_messages read_type=%s error=%s",
            read_type, exc,
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
    except Exception as exc:
        logger.exception("notifications: kb read forward failed operation=mark_message_read error=%s", exc)
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
    except Exception as exc:
        logger.exception("notifications: kb delete forward failed operation=delete_readed_message error=%s", exc)
    return 0


def _mark_skill_publish_read(
    account: str, event_ids: list[str], *, uuid: str = ""
) -> int:
    """Forward skill_publish event IDs to upstream mark-read."""
    ids = _parse_kb_message_ids(event_ids)
    if not ids or not skill_publish_enabled() or not account:
        return 0
    ext_id = uuid or account
    try:
        from integration.config import skill_publish_platform
        from integration.skill_publish.client import (
            mark_external_notifications_read,
        )

        ok = mark_external_notifications_read(
            platform=skill_publish_platform(),
            external_user_id=ext_id,
            event_ids=ids,
        )
        return len(ids) if ok else 0
    except Exception as exc:
        logger.exception(
            "notifications: skill_publish read forward failed error=%s", exc
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

        _sync_skill_publish(account, uuid=uuid)
        items = _fetch_skill_publish_events(
            account=account, uuid=uuid, read_type=read_type, limit=limit + 1
        )
        items.extend(_fetch_kb_messages(
            account=account,
            uuid=uuid,
            read_type=read_type,
            limit=limit + 1,
            cursor=cursor,
        ))
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

        _sync_skill_publish(account, uuid=uuid)
        kb_summary = _fetch_kb_summary(
            account=account,
            uuid=uuid,
            read_type=read_type,
        )
        sp_summary = _fetch_skill_publish_summary(
            account=account, uuid=uuid, read_type=read_type
        )

        by_category = dict(kb_summary.get("by_category") or {})
        for category, counts in (sp_summary.get("by_category") or {}).items():
            merged = by_category.setdefault(category, {"total": 0, "unread": 0})
            merged["total"] += counts.get("total") or 0
            merged["unread"] += counts.get("unread") or 0

        j(handler, {
            "total": (kb_summary.get("total") or 0) + (sp_summary.get("total") or 0),
            "unread": (kb_summary.get("unread") or 0) + (sp_summary.get("unread") or 0),
            "by_category": by_category,
        })
        return True

    return False


def try_handle_post(handler, parsed, body) -> bool:
    """Handle POST requests for notifications."""
    path = parsed.path

    if path == "/api/integration/notifications/read":
        raw_ids = body.get("ids", [])
        ids = raw_ids if isinstance(raw_ids, list) else []

        kb_ids = _extract_kb_ids(ids)
        sp_ids = _extract_skill_publish_ids(ids)
        account = str(body.get("account", "") or "").strip()
        uuid = str(body.get("uuid", "") or "").strip()

        updated = _mark_kb_messages_read(kb_ids) if kb_ids and account and uuid else 0
        updated += _mark_skill_publish_read(account, sp_ids, uuid=uuid)

        j(handler, {"ok": True, "updated": updated})
        return True

    if path == "/api/integration/notifications/delete":
        raw_ids = body.get("ids", [])
        ids = raw_ids if isinstance(raw_ids, list) else []

        kb_ids = _extract_kb_ids(ids)
        account = str(body.get("account", "") or "").strip()
        uuid = str(body.get("uuid", "") or "").strip()

        # skill_publish notifications live upstream only; deletion is not
        # supported until the upstream contract provides it.
        deleted = _delete_kb_messages(kb_ids) if kb_ids and account and uuid else 0

        j(handler, {"ok": True, "deleted": deleted})
        return True

    return False
