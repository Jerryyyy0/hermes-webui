"""Diagnostics for control rows received from Hermes Agent."""

from __future__ import annotations

import logging
from typing import Any

from integration.agent_message_semantics.classifier import control_message_semantics


logger = logging.getLogger(__name__)


def log_control_message(action: str, message: Any) -> None:
    """Log the classified control row, including its model-facing text."""
    if not isinstance(message, dict):
        return
    resolved = control_message_semantics(message)
    if resolved is None:
        return
    message_class, kind = resolved
    logger.info(
        "hermes_message_semantics action=%s class=%s kind=%s role=%s content=%r api_content=%r",
        action,
        message_class,
        kind,
        str(message.get("role") or "unknown"),
        message.get("content"),
        message.get("api_content"),
    )
