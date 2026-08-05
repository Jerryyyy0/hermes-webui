"""Visible-transcript projection for Agent-owned internal messages."""

from __future__ import annotations

from typing import Any

from integration.agent_message_semantics.audit import log_control_message
from integration.agent_message_semantics.classifier import is_non_anchor_control_message


def drop_non_display_messages(messages: Any, *, action: str = "display_drop") -> list:
    """Return a shallow display copy without Agent-only context/control rows."""
    retained = []
    for message in list(messages or []):
        if is_non_anchor_control_message(message):
            log_control_message(action, message)
            continue
        retained.append(message)
    return retained


project_messages_for_display = drop_non_display_messages
