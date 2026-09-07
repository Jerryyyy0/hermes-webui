from api.models import merge_session_messages_append_only, reconcile_message_timestamps


def test_reconciles_four_message_transcript_without_copying_db_metadata():
    sidecar = [
        {"role": "user", "content": "hello", "timestamp": 10.0},
        {"role": "assistant", "content": "", "timestamp": 10.000001},
        {"role": "tool", "content": "result", "tool_call_id": "call-1", "timestamp": 10.000002},
        {"role": "assistant", "content": "done", "timestamp": 10.000003},
    ]
    state = [
        {"_state_db_id": 1, "role": "user", "content": "hello", "timestamp": 1.1},
        {"_state_db_id": 2, "role": "assistant", "content": "", "timestamp": 2.2},
        {"_state_db_id": 3, "role": "tool", "content": "result", "tool_call_id": "call-1", "timestamp": 3.3},
        {"_state_db_id": 4, "role": "assistant", "content": "done", "timestamp": 4.4, "extra": "db-only"},
    ]
    result = reconcile_message_timestamps(sidecar, state)
    assert [m["timestamp"] for m in result["messages"]] == [1.1, 2.2, 3.3, 4.4]
    assert result["updated"] == 4
    assert "extra" not in result["messages"][-1]
    assert sidecar[0]["timestamp"] == 10.0


def test_workspace_prefix_and_repeated_turns_align_in_order():
    sidecar = [
        {"role": "user", "content": "same", "timestamp": 10},
        {"role": "assistant", "content": "ok", "timestamp": 11},
        {"role": "user", "content": "same", "timestamp": 20},
    ]
    state = [
        {"role": "user", "content": "[Workspace::v1: /tmp]\nsame", "timestamp": 1},
        {"role": "assistant", "content": "ok", "timestamp": 2},
        {"role": "user", "content": "[Workspace::v1: /tmp]\nsame", "timestamp": 3},
    ]
    result = reconcile_message_timestamps(sidecar, state)
    assert [m["timestamp"] for m in result["messages"]] == [1, 2, 3]


def test_mismatched_duplicate_counts_fail_closed():
    sidecar = [{"role": "user", "content": "same", "timestamp": 10}]
    state = [
        {"role": "user", "content": "same", "timestamp": 1},
        {"role": "user", "content": "same", "timestamp": 2},
    ]
    result = reconcile_message_timestamps(sidecar, state)
    assert result["messages"][0]["timestamp"] == 10
    assert result["ambiguous"] == 1


def test_tool_call_identity_prevents_wrong_timestamp():
    sidecar = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a"}], "timestamp": 10}]
    state = [{"role": "assistant", "content": "", "tool_calls": [{"id": "b"}], "timestamp": 1}]
    result = reconcile_message_timestamps(sidecar, state)
    assert result["messages"][0]["timestamp"] == 10
    assert result["matched"] == 0


def test_append_only_merge_uses_db_timestamp_without_mutating_sidecar():
    sidecar = [{"role": "user", "content": "hello", "timestamp": 10}]
    state = [{"role": "user", "content": "hello", "timestamp": 1}]
    merged = merge_session_messages_append_only(sidecar, state)
    assert merged[0]["timestamp"] == 1
    assert sidecar[0]["timestamp"] == 10
