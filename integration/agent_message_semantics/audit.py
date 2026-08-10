"""Diagnostics for control rows received from Hermes Agent."""

from __future__ import annotations

import logging
from typing import Any

from integration.agent_message_semantics.classifier import control_message_semantics


logger = logging.getLogger(__name__)

def log_message_event(
    action: str,
    message: Any,
    *,
    message_class: str,
    kind: str,
    session_id: str | None = None,
) -> None:
    """Log a display-projection decision for any classified message row."""
    if not isinstance(message, dict):
        return
    logger.debug(
        "hermes_message_semantics action=%s class=%s kind=%s role=%s "
        "session_id=%s turn_key_present=%s",
        action,
        message_class,
        kind,
        str(message.get("role") or "unknown"),
        session_id or "",
        bool(message.get("_turn_key")),
    )


def log_control_message(
    action: str,
    message: Any,
    *,
    session_id: str | None = None,
) -> None:
    """Log classified control-row metadata without model-facing text."""
    if not isinstance(message, dict):
        return
    resolved = control_message_semantics(message)
    if resolved is None:
        return
    message_class, kind = resolved
    log_message_event(
        action,
        message,
        message_class=message_class,
        kind=kind,
        session_id=session_id,
    )
