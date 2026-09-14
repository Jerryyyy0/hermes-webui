"""A fuzzy history match must never turn a human prompt into scaffolding."""

import copy

import pytest

from api.models import merge_session_messages_append_only
from api.routes import _message_window_for_display
from integration.agent_message_semantics.projection import drop_non_display_messages


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("same_id", [False, True])
def test_summary_containing_transcript_text_cannot_hide_original(role, same_id):
    # Captured failure shape: a committed prompt also occurs inside a later
    # compaction summary; fuzzy replay matching copies the summary's semantics.
    original = {"role": role, "content": "生成源数据并检查异常", "_turn_key": "turn:1"}
    summary = {
        "role": role,
        "content": "[CONTEXT COMPACTION] 用户要求：生成源数据并检查异常；工作已完成。",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": "compaction_summary",
    }
    if same_id:
        original["id"] = summary["id"] = 1
    sidecar = [original, {"role": "assistant", "content": "已生成源数据"}]
    before = copy.deepcopy(sidecar)
    merged = merge_session_messages_append_only(sidecar, [summary])
    visible = drop_non_display_messages(merged)
    window, offset = _message_window_for_display(
        visible, msg_limit=50, msg_before=len(visible), turn_align=True,
    )
    assert window == before
    assert offset == 0
    assert sidecar == before


def test_fuzzy_match_cannot_assign_another_turn_key():
    original = {"role": "user", "content": "原始提问"}
    merged = merge_session_messages_append_only([original], [
        {"role": "user", "content": "总结：原始提问", "_turn_key": "turn:9"},
    ])
    assert "_turn_key" not in merged[0]


@pytest.mark.parametrize("prefix", ["", "[Workspace::v1: /workspace]\n"])
def test_exact_replayed_control_retains_provenance(prefix):
    merged = merge_session_messages_append_only([
        {"role": "user", "content": "任务已完成"},
    ], [{
        "role": "user", "content": prefix + "任务已完成",
        "_turn_key": "turn:1",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": "async_delegation_completion",
    }])
    assert merged[0]["_turn_key"] == "turn:1"
    assert drop_non_display_messages(merged) == []
