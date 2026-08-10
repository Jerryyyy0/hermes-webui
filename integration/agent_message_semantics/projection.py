"""Visible-transcript projection for Agent-owned internal messages."""

from __future__ import annotations

import copy
import json
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
_REPLACED_ASSISTANT_FINISH_REASONS = {
    "verification_required",
    "verify_hook_continue",
    "incomplete",
}


def _turn_key(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("_turn_key") or "").strip()


def _user_semantic_identity(message: dict) -> str:
    """Return the visible user payload used to distinguish a replay from a conflict."""
    payload = {
        "content": message.get("content"),
        "attachments": message.get("attachments"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr)


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
    canonical_by_identity: dict[tuple[str, str], dict] = {}
    canonical_index_by_identity: dict[tuple[str, str], int] = {}
    identities_by_turn: dict[str, set[str]] = {}
    for message in messages:
        if not isinstance(message, dict):
            retained.append(message)
            continue
        if str(message.get("role") or "").lower() != "user":
            retained.append(message)
            continue
        turn_key = _turn_key(message)
        semantic_identity = _user_semantic_identity(message)
        identity_key = (turn_key, semantic_identity)
        if not turn_key or identity_key not in canonical_by_identity:
            if turn_key and identities_by_turn.get(turn_key):
                log_message_event(
                    "turn_key_conflict",
                    message,
                    message_class="ordinary_user",
                    kind="user_turn_key_content_conflict",
                    session_id=session_id,
                )
            retained_message = message
            if turn_key:
                canonical_by_identity[identity_key] = retained_message
                canonical_index_by_identity[identity_key] = len(retained)
                identities_by_turn.setdefault(turn_key, set()).add(semantic_identity)
            retained.append(retained_message)
            continue

        canonical = canonical_by_identity[identity_key]
        # Do not mutate the session's source row while carrying metadata from
        # a replay copy into the display-only projection.
        if canonical is message:
            continue
        canonical_copy = dict(canonical)
        _merge_display_metadata(canonical_copy, message)
        retained[canonical_index_by_identity[identity_key]] = canonical_copy
        canonical_by_identity[identity_key] = canonical_copy
        log_message_event(
            "display_duplicate_drop",
            message,
            message_class="ordinary_user",
            kind="async_origin_user_replay",
            session_id=session_id,
        )
    return retained


def _attach_background_task_ids(
    messages: list,
    origins: Any,
    *,
    session_id: str | None = None,
) -> list:
    """Attach sidecar delegation IDs to the display copy of origin users."""
    if not isinstance(origins, dict):
        return messages
    ids_by_turn: dict[str, list[str]] = {}
    for delegation_id, record in origins.items():
        if not isinstance(record, dict):
            continue
        turn_key = str(record.get("turn_key") or "").strip()
        delegation_id = str(delegation_id or "").strip()
        if turn_key and delegation_id:
            ids_by_turn.setdefault(turn_key, []).append(delegation_id)
    if not ids_by_turn:
        return messages

    projected = []
    for message in messages:
        if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
            projected.append(message)
            continue
        turn_key = _turn_key(message)
        delegation_ids = ids_by_turn.get(turn_key)
        if not delegation_ids:
            projected.append(message)
            continue
        existing_ids = message.get("_background_task_ids")
        merged_ids = []
        if isinstance(existing_ids, list):
            merged_ids.extend(str(value).strip() for value in existing_ids if str(value).strip())
        merged_ids.extend(value for value in delegation_ids if value not in merged_ids)
        if not merged_ids or existing_ids == merged_ids:
            projected.append(message)
            continue
        display_message = dict(message)
        display_message["_background_task_ids"] = merged_ids
        projected.append(display_message)
        log_message_event(
            "origin_metadata_attach",
            display_message,
            message_class="ordinary_user",
            kind="async_origin_user",
            session_id=session_id,
        )
    return projected


def _is_text_assistant(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and str(message.get("role") or "").lower() == "assistant"
        and not message.get("tool_calls")
    )


def _has_display_content(message: dict) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (list, dict)):
        return bool(content)
    return content is not None


def _merge_assistant_messages(partial: dict, continuation: dict) -> dict | None:
    partial_content = partial.get("content", "")
    continuation_content = continuation.get("content", "")
    merged = copy.deepcopy(continuation)
    if isinstance(partial_content, str) and isinstance(continuation_content, str):
        merged["content"] = partial_content + continuation_content
    elif isinstance(partial_content, list) and isinstance(continuation_content, list):
        merged["content"] = copy.deepcopy(partial_content) + copy.deepcopy(continuation_content)
    elif isinstance(partial_content, list) and isinstance(continuation_content, str):
        merged["content"] = copy.deepcopy(partial_content)
        if continuation_content:
            merged["content"].append({"type": "text", "text": continuation_content})
    elif isinstance(partial_content, str) and isinstance(continuation_content, list):
        blocks = []
        if partial_content:
            blocks.append({"type": "text", "text": partial_content})
        blocks.extend(copy.deepcopy(continuation_content))
        merged["content"] = blocks
    else:
        return None
    return merged


def _project_one_answer_messages(
    messages: Any,
    *,
    action: str,
    session_id: str | None,
) -> list:
    retained: list = []
    pending_answer_index: int | None = None
    pending_answer_mode = ""

    for message in list(messages or []):
        if is_non_anchor_control_message(message):
            log_control_message(action, message, session_id=session_id)
            continue

        role = str(message.get("role") or "").lower() if isinstance(message, dict) else ""
        if role == "user":
            pending_answer_index = None
            pending_answer_mode = ""
            retained.append(message)
            continue

        if _is_text_assistant(message):
            finish_reason = str(message.get("finish_reason") or "").strip()
            if finish_reason == "length":
                projected_message = message
                if pending_answer_index is not None:
                    previous = retained[pending_answer_index]
                    if pending_answer_mode == "append":
                        merged = _merge_assistant_messages(previous, message)
                        if merged is not None:
                            projected_message = merged
                    retained.pop(pending_answer_index)
                retained.append(projected_message)
                pending_answer_index = len(retained) - 1
                pending_answer_mode = "append"
                continue
            if finish_reason in _REPLACED_ASSISTANT_FINISH_REASONS:
                if not _has_display_content(message):
                    continue
                if pending_answer_index is not None:
                    retained.pop(pending_answer_index)
                retained.append(message)
                pending_answer_index = len(retained) - 1
                pending_answer_mode = "replace"
                continue
            projected_message = message
            if pending_answer_index is not None:
                previous = retained[pending_answer_index]
                if pending_answer_mode == "append":
                    merged = _merge_assistant_messages(previous, message)
                    if merged is None:
                        pending_answer_index = None
                        pending_answer_mode = ""
                        retained.append(message)
                        continue
                    projected_message = merged
                retained.pop(pending_answer_index)
                pending_answer_index = None
                pending_answer_mode = ""
            retained.append(projected_message)
            continue

        retained.append(message)

    return retained


def drop_non_display_messages(
    messages: Any,
    *,
    action: str = "display_drop",
    session_id: str | None = None,
    background_task_origins: Any = None,
) -> list:
    """Return a display projection without Agent-only rows or replay users."""
    retained = _project_one_answer_messages(
        messages,
        action=action,
        session_id=session_id,
    )
    retained = _dedupe_replayed_users(retained, session_id=session_id)
    return _attach_background_task_ids(
        retained,
        background_task_origins,
        session_id=session_id,
    )


project_messages_for_display = drop_non_display_messages
