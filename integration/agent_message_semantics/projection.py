"""Visible-transcript projection for Agent-owned internal messages."""

from __future__ import annotations

import copy
from typing import Any

from integration.agent_message_semantics.audit import log_control_message, log_message_event
from integration.agent_message_semantics.classifier import is_non_anchor_control_message


_DISPLAY_METADATA_KEYS = (
    "_background_task_ids",
    "_turnDuration",
    "_turnTps",
    "_turnUsage",
    "_firstTokenMs",
    "_usedModel",
    "_gatewayRouting",
    "_statusCard",
    "_anchor_stream_id",
    "_anchor_activity_scene",
)


def _turn_key(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("_turn_key") or "").strip()


def _merge_display_metadata(canonical: dict, replay: dict) -> None:
    """Copy only display metadata missing from the canonical user row."""
    for key in _DISPLAY_METADATA_KEYS:
        value = replay.get(key)
        if value in (None, "", [], {}, ()) or canonical.get(key) not in (None, "", [], {}, ()):
            continue
        canonical[key] = copy.deepcopy(value)


def _dedupe_replayed_users(messages: list, *, session_id: str | None = None) -> list:
    """Hide replayed user rows while preserving same-text messages in new turns."""
    retained = []
    canonical_by_turn: dict[str, dict] = {}
    canonical_index_by_turn: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            retained.append(message)
            continue
        if str(message.get("role") or "").lower() != "user":
            retained.append(message)
            continue
        turn_key = _turn_key(message)
        if not turn_key or turn_key not in canonical_by_turn:
            retained_message = message
            if turn_key:
                canonical_by_turn[turn_key] = retained_message
                canonical_index_by_turn[turn_key] = len(retained)
            retained.append(retained_message)
            continue

        canonical = canonical_by_turn[turn_key]
        # Do not mutate the session's source row while carrying metadata from
        # a replay copy into the display-only projection.
        if canonical is message:
            continue
        canonical_copy = dict(canonical)
        _merge_display_metadata(canonical_copy, message)
        retained[canonical_index_by_turn[turn_key]] = canonical_copy
        canonical_by_turn[turn_key] = canonical_copy
        log_message_event(
            "display_duplicate_drop",
            message,
            message_class="ordinary_user",
            kind="async_origin_user_replay",
            session_id=session_id,
        )
    return retained


def drop_non_display_messages(
    messages: Any,
    *,
    action: str = "display_drop",
    session_id: str | None = None,
) -> list:
    """Return a display projection without Agent-only rows or replay users."""
    retained = []
    for message in list(messages or []):
        if is_non_anchor_control_message(message):
            log_control_message(action, message, session_id=session_id)
            continue
        retained.append(message)
    return _dedupe_replayed_users(retained, session_id=session_id)


project_messages_for_display = drop_non_display_messages
