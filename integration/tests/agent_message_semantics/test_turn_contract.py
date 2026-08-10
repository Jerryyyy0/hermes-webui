"""Every Agent message kind obeys the WebUI turn-ownership contract."""

from types import SimpleNamespace

import pytest

from api import streaming
from api.session_manifest import _message_turns


INTERNAL_SCAFFOLD_KINDS = (
    "empty_response_recovery",
    "thinking_prefill",
    "verification_stop",
    "pre_verify",
    "kanban_stop",
)
CONTEXT_ANCHOR_KINDS = (
    "length_continuation",
    "codex_incomplete_nudge",
    "intent_ack_continuation",
    "max_iteration_summary_request",
    "todo_snapshot",
    "compaction_summary",
    "compression_no_user_anchor",
    "mcp_reload",
    "model_switch",
    "personality_pivot",
)
LEGACY_INTERNAL_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",
)


def _real_turn_with(control_message):
    return [
        {"role": "user", "content": "create artifact", "_turn_key": "turn:8"},
        {"role": "assistant", "content": "interim real answer"},
        control_message,
        {"role": "assistant", "content": "final real answer: MEDIA: result.png"},
    ]


@pytest.mark.parametrize("kind", INTERNAL_SCAFFOLD_KINDS)
def test_every_internal_scaffold_kind_keeps_real_turn_and_assistants(kind):
    messages = _real_turn_with({
        "role": "user",
        "content": "private-control-message",
        "_hermes_message_class": "internal_scaffold",
        "_hermes_scaffold_kind": kind,
    })

    visible = streaming._drop_synthetic_control_messages(messages)
    assert [row["content"] for row in visible] == [
        "create artifact",
        "interim real answer",
        "final real answer: MEDIA: result.png",
    ]
    binding = streaming._latest_user_turn_binding(
        SimpleNamespace(messages=messages), "create artifact", "turn:8"
    )
    assert binding["status"] == "valid"
    turns = _message_turns(messages)
    assert [(turn["turn_key"], turn["start_msg_idx"], turn["end_msg_idx"]) for turn in turns] == [
        ("turn:8", 0, 3),
    ]


@pytest.mark.parametrize("kind", CONTEXT_ANCHOR_KINDS)
def test_every_context_anchor_kind_is_hidden_without_losing_real_turn(kind):
    messages = _real_turn_with({
        "role": "user",
        "content": "private-model-only-context",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": kind,
    })

    visible = streaming._drop_synthetic_control_messages(messages)
    assert [row["content"] for row in visible] == [
        "create artifact",
        "interim real answer",
        "final real answer: MEDIA: result.png",
    ]
    binding = streaming._latest_user_turn_binding(
        SimpleNamespace(messages=messages), "create artifact", "turn:8"
    )
    assert binding["status"] == "valid"
    turns = _message_turns(messages)
    assert [(turn["turn_key"], turn["start_msg_idx"], turn["end_msg_idx"]) for turn in turns] == [
        ("turn:8", 0, 3),
    ]


def test_assistant_role_compaction_summary_is_hidden_without_losing_real_turn():
    messages = _real_turn_with({
        "role": "assistant",
        "content": "private-model-only-summary",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": "compaction_summary",
    })

    visible = streaming._drop_synthetic_control_messages(messages)
    assert [row["content"] for row in visible] == [
        "create artifact",
        "interim real answer",
        "final real answer: MEDIA: result.png",
    ]
    binding = streaming._latest_user_turn_binding(
        SimpleNamespace(messages=messages), "create artifact", "turn:8"
    )
    assert binding["status"] == "valid"
    turns = _message_turns(messages)
    assert [(turn["turn_key"], turn["start_msg_idx"], turn["end_msg_idx"]) for turn in turns] == [
        ("turn:8", 0, 3),
    ]


@pytest.mark.parametrize("legacy_flag", LEGACY_INTERNAL_FLAGS)
def test_all_legacy_internal_flags_remain_webui_compatible(legacy_flag):
    message = {"role": "user", "content": "legacy control", legacy_flag: True}

    assert streaming._is_synthetic_control_message(message) is True
