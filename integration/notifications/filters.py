"""Filter helpers for notification list queries."""

from integration.notifications.constants import KbMassType


def is_apply_result_pending(item: dict) -> bool:
    """Return True for massType=3, state=2 applicant result-pending messages."""
    meta = item.get("metadata")
    if not isinstance(meta, dict):
        return False
    mass_type = meta.get("massType")
    state = meta.get("state")
    try:
        return int(mass_type) == KbMassType.APPLY_RESULT and int(state) == 2
    except (TypeError, ValueError):
        return False


def exclude_apply_result_pending(items: list[dict]) -> list[dict]:
    """Drop applicant result-pending messages from list responses."""
    return [item for item in items if not is_apply_result_pending(item)]


def filter_by_action_status(items: list[dict], action_status_param: str | None) -> list[dict]:
    """Keep items whose action_status is in the comma-separated allow-list."""
    if not action_status_param:
        return items
    allowed = {s.strip() for s in action_status_param.split(",") if s.strip()}
    if not allowed:
        return items
    return [item for item in items if item.get("action_status", "") in allowed]
