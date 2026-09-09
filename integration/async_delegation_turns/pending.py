"""Public projection for core pending fields owned by server wakeups."""

from __future__ import annotations

from typing import Any


def public_pending_state(session: Any, *, include_attachments: bool = False) -> dict[str, Any]:
    """Hide internal wakeup prose while preserving truthful busy metadata."""
    source = str(getattr(session, "pending_user_source", None) or "").strip() or None
    has_pending = bool(getattr(session, "pending_user_message", None))
    visible = has_pending and source != "async_delegation_wakeup"
    projected = {
        "pending_user_message": getattr(session, "pending_user_message", None) if visible else None,
        "pending_user_source": source,
        "pending_turn_key": str(getattr(session, "pending_turn_key", None) or "").strip() or None,
        "pending_user_visible": bool(visible),
        "has_pending_user_message": has_pending,
    }
    if include_attachments:
        projected["pending_attachments"] = (
            list(getattr(session, "pending_attachments", None) or []) if visible else []
        )
    return projected
