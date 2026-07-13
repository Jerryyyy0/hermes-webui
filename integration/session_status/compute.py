"""Pure derivation of session-list status fields."""

from __future__ import annotations

from typing import Any


def to_timestamp(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed >= 0 else 0.0


def to_optional_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def latest_activity_at(row: dict) -> float:
    return max(
        to_timestamp(row.get("last_message_at")),
        to_optional_timestamp(row.get("last_error_at")) or 0.0,
    )


def compute_session_status(row: dict, read_until: float) -> dict[str, object]:
    """Return the two public status fields for one sidebar row."""

    last_error_at = to_optional_timestamp(row.get("last_error_at"))
    is_unread = latest_activity_at(row) > to_timestamp(read_until)

    if last_error_at is not None:
        status = "error"
    elif (
        row.get("is_streaming")
        or row.get("active_stream_id")
        or row.get("pending_user_message")
    ):
        status = "in_progress"
    elif is_unread:
        status = "has_new_messages"
    else:
        status = "ready"

    return {"status": status, "is_unread": is_unread}
