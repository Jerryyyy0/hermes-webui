"""Normalize downstream KB messages into notification items."""

from datetime import datetime

from integration.notifications.constants import (
    KB_OPENABLE_MASS_TYPES,
    KB_STATE_TO_ACTION_STATUS,
    NotificationCategory,
    NotificationStatus,
)

KB_ID_PREFIX = "kb:"


def parse_created_at(msg: dict) -> float:
    """Parse downstream createTime (ISO string) to unix timestamp."""
    raw = msg.get("createTime")
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def parse_mass_type(msg: dict) -> int | None:
    """Parse downstream massType; return None when missing or invalid."""
    raw = msg.get("massType")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def map_kb_action_status(raw_state) -> str:
    """Map downstream state to unified action_status string."""
    if raw_state is None:
        return ""
    try:
        key = int(raw_state)
    except (TypeError, ValueError):
        return ""
    return KB_STATE_TO_ACTION_STATUS.get(key, "")


def is_actionable(msg: dict) -> int:
    """Return 1 when the client may open a detail view (join requests only)."""
    mass_type = parse_mass_type(msg)
    return 1 if mass_type in KB_OPENABLE_MASS_TYPES else 0


def map_kb_read_status(
    msg: dict,
    *,
    read_type: str,
    seen_ids: set[str] | None = None,
) -> str:
    """Derive notification read status from query read_type and optional seen ID set."""
    if read_type == "unread":
        return NotificationStatus.UNREAD
    if read_type == "seen":
        return NotificationStatus.READ
    kb_id = str(msg.get("id", "") or "")
    if seen_ids and kb_id in seen_ids:
        return NotificationStatus.READ
    return NotificationStatus.UNREAD


def normalize_kb_message(
    msg: dict,
    *,
    read_type: str = "all",
    seen_ids: set[str] | None = None,
) -> dict:
    """Normalize a downstream knowledge base message to notification format."""
    kb_id = str(msg.get("id", "") or "")
    created_at = parse_created_at(msg)
    show_name = msg.get("showName")
    source = str(show_name) if show_name is not None else ""
    mass_type = parse_mass_type(msg)
    if mass_type in KB_OPENABLE_MASS_TYPES:
        action_status = map_kb_action_status(msg.get("state"))
    else:
        action_status = ""
    return {
        "id": f"{KB_ID_PREFIX}{kb_id}",
        "category": NotificationCategory.KB_APPLY,
        "title": str(msg.get("massage") or ""),
        "body": "",
        "source": source,
        "ref_id": kb_id,
        "status": map_kb_read_status(msg, read_type=read_type, seen_ids=seen_ids),
        "priority": "normal",
        "actionable": is_actionable(msg),
        "action_status": action_status,
        "metadata": msg,
        "created_at": created_at,
        "updated_at": created_at,
    }
