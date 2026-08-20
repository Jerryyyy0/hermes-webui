"""Normalize downstream KB messages and skill-publish events into notification items."""

from datetime import datetime

from integration.notifications.constants import (
    KB_OPENABLE_MASS_TYPES,
    KB_STATE_TO_ACTION_STATUS,
    NotificationCategory,
    NotificationIdPrefix,
    NotificationStatus,
)

KB_ID_PREFIX = "kb:"
SKILL_PUBLISH_ID_PREFIX = NotificationIdPrefix.SKILL_PUBLISH


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


def _parse_event_time(event: dict) -> float:
    """Parse upstream createTime (``yyyy-MM-dd HH:mm:ss`` or ISO) to epoch."""
    raw = event.get("createTime")
    if not raw:
        return 0.0
    text = str(raw)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _get_application_type(application_id: str) -> str:
    """Look up application_type from local store by upstream application_id."""
    from integration.skill_publish.constants import ApplicationType
    from integration.skill_publish import store

    if not application_id:
        return ApplicationType.PUBLISH
    try:
        with store._connect(None) as conn:
            row = conn.execute(
                "SELECT application_type FROM skill_application_forms"
                " WHERE upstream_application_id = ? LIMIT 1",
                (application_id,),
            ).fetchone()
        if row and row["application_type"]:
            return row["application_type"]
    except Exception:
        pass
    return ApplicationType.PUBLISH


def normalize_skill_publish_event(
    event: dict,
    *,
    read_type: str = "all",
    seen_ids: set[str] | None = None,
) -> dict:
    """Normalize an upstream skill-publish audit event to notification format.

    Read state is owned by upstream SkillHub: ``read_type`` derives the status
    the same way KB messages do (with ``seen_ids`` for read_type=all).
    """
    event_id = str(event.get("id", "") or "")
    scene = str(event.get("scene") or "")
    skill_name = str(event.get("skillName") or event.get("name"))
    created_at = _parse_event_time(event)

    if scene == "3":
        title = f"技能[{skill_name}]已被管理员下架"
        action_status = ""
        actionable = 0
    elif scene == "4":
        title = f"技能[{skill_name}]已被管理员重新上架"
        action_status = ""
        actionable = 0
    else:
        from integration.skill_publish.constants import ApplicationType
        approved = str(event.get("result")) == "1"
        application_id = str(event.get("applicationId") or "")
        app_type = _get_application_type(application_id)
        type_label = "下架" if app_type == ApplicationType.UNPUBLISH else "发布"
        result_label = "通过审批" if approved else "被驳回"
        title = f"您申请{type_label}的{skill_name}技能已{result_label}"
        action_status = "approved" if approved else "rejected"
        actionable = 1

    return {
        "id": f"{SKILL_PUBLISH_ID_PREFIX}{event_id}",
        "category": NotificationCategory.SKILL_PUBLISH,
        "title": title,
        "body": str(event.get("auditComment") or ""),
        "source": skill_name,
        "ref_id": str(event.get("applicationId") or ""),
        "status": _derive_event_read_status(
            event_id, read_type=read_type, seen_ids=seen_ids
        ),
        "priority": "normal",
        "actionable": actionable,
        "action_status": action_status,
        "metadata": {"id": event_id},
        "created_at": created_at,
        "updated_at": created_at,
    }


def _derive_event_read_status(
    event_id: str, *, read_type: str, seen_ids: set[str] | None
) -> str:
    if read_type == "unread":
        return NotificationStatus.UNREAD
    if read_type == "seen":
        return NotificationStatus.READ
    if seen_ids and event_id in seen_ids:
        return NotificationStatus.READ
    return NotificationStatus.UNREAD
