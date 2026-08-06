"""Diagnostics for control rows received from Hermes Agent."""

from __future__ import annotations

import logging
import os
from typing import Any

from integration.agent_message_semantics.classifier import control_message_semantics


logger = logging.getLogger(__name__)

_DEFAULT_LOG_CONTENT_MAX_CHARS = 500


def _log_content_max_chars() -> int:
    """Max chars for audit log content; <=0 disables truncation."""
    raw = (os.getenv("HERMES_MESSAGE_SEMANTICS_LOG_MAX_CHARS") or "").strip()
    if not raw:
        return _DEFAULT_LOG_CONTENT_MAX_CHARS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_LOG_CONTENT_MAX_CHARS


def _log_content_preview(value: Any) -> Any:
    """Truncate long strings for logs; leave non-strings and short text alone."""
    if not isinstance(value, str):
        return value
    max_chars = _log_content_max_chars()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return f"{value[:max_chars]}…(+{omitted} chars)"


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
    logger.info(
        "hermes_message_semantics action=%s class=%s kind=%s role=%s content=%r "
        "api_content=%r session_id=%s turn_key=%s",
        action,
        message_class,
        kind,
        str(message.get("role") or "unknown"),
        _log_content_preview(message.get("content")),
        _log_content_preview(message.get("api_content")),
        session_id or "",
        message.get("_turn_key") or "",
    )


def log_control_message(
    action: str,
    message: Any,
    *,
    session_id: str | None = None,
) -> None:
    """Log the classified control row, including its model-facing text."""
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
