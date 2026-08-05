"""Recognize Hermes Agent control rows without matching their private text."""

from __future__ import annotations

from typing import Any


INTERNAL_SCAFFOLD_CLASS = "internal_scaffold"
CONTEXT_ANCHOR_CLASS = "context_anchor"
_LEGACY_INTERNAL_FLAGS = {
    "_empty_recovery_synthetic": "empty_response_recovery",
    "_empty_terminal_sentinel": "empty_response_recovery",
    "_thinking_prefill": "thinking_prefill",
    "_verification_stop_synthetic": "verification_stop",
    "_pre_verify_synthetic": "pre_verify",
    "_kanban_stop_synthetic": "kanban_stop",
}
_LEGACY_CONTEXT_FLAGS = {"_todo_snapshot_synthetic": "todo_snapshot"}


def control_message_semantics(message: Any) -> tuple[str, str] | None:
    """Return the resolved class/kind without examining private message text."""
    if not isinstance(message, dict):
        return None
    message_class = message.get("_hermes_message_class")
    kind = message.get("_hermes_scaffold_kind")
    if message_class in {INTERNAL_SCAFFOLD_CLASS, CONTEXT_ANCHOR_CLASS}:
        return str(message_class), str(kind or "unknown")
    for flag, legacy_kind in _LEGACY_INTERNAL_FLAGS.items():
        if message.get(flag):
            return INTERNAL_SCAFFOLD_CLASS, legacy_kind
    for flag, legacy_kind in _LEGACY_CONTEXT_FLAGS.items():
        if message.get(flag):
            return CONTEXT_ANCHOR_CLASS, legacy_kind
    return None


def is_internal_scaffold(message: Any) -> bool:
    resolved = control_message_semantics(message)
    return bool(resolved and resolved[0] == INTERNAL_SCAFFOLD_CLASS)


def is_context_anchor(message: Any) -> bool:
    resolved = control_message_semantics(message)
    return bool(resolved and resolved[0] == CONTEXT_ANCHOR_CLASS)


def is_non_anchor_control_message(message: Any) -> bool:
    """Return whether a row is internal or model-only context, never user intent."""
    return is_internal_scaffold(message) or is_context_anchor(message)
