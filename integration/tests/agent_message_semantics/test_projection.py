"""Display projection regressions for replayed Agent user rows."""

import copy

from integration.agent_message_semantics.projection import drop_non_display_messages


def test_same_turn_replayed_user_is_hidden_and_metadata_is_kept(caplog):
    canonical = {
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
    }
    replay = {
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
        "_background_task_ids": ["deleg_123"],
    }

    with caplog.at_level("DEBUG"):
        visible = drop_non_display_messages(
            [canonical, replay],
            session_id="session-1",
        )

    assert visible == [{
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
    }]
    assert canonical == {
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
    }
    assert "action=display_duplicate_drop" in caplog.text
    assert "kind=async_origin_user_replay" in caplog.text
    assert "content=" not in caplog.text


def test_background_task_ids_are_not_exposed_when_async_delegations_are_available():
    message = {
        "role": "user",
        "content": "派发后台任务",
        "_turn_key": "turn:8",
        "_background_task_ids": ["deleg_123"],
        "async_delegations": {"state": "running", "items": {"deleg_123": {}}},
    }

    visible = drop_non_display_messages([message])

    assert "_background_task_ids" not in visible[0]
    assert visible[0]["async_delegations"]["items"] == {"deleg_123": {}}


def test_same_content_in_different_turns_is_preserved():
    messages = [
        {"role": "user", "content": "你好", "_turn_key": "turn:8"},
        {"role": "user", "content": "你好", "_turn_key": "turn:9"},
    ]

    visible = drop_non_display_messages(messages)

    assert visible == messages


def test_same_turn_key_with_different_user_content_is_preserved_and_audited(caplog):
    weather_request = {
        "role": "user",
        "content": "帮我设置定时任务，每三分钟获取今日杭州天气，并生成一个 Markdown 文件。",
        "_turn_key": "turn:3",
    }
    presentation_request = {
        "role": "user",
        "content": "[Workspace::v1: /tmp/workspace]\n请根据大纲制作 PPT 讲稿和 HTML 演示稿。\n[Attached files: 集团简介.docx]",
        "_turn_key": "turn:3",
    }

    with caplog.at_level("DEBUG"):
        visible = drop_non_display_messages(
            [weather_request, presentation_request],
            session_id="session-aggregate",
        )

    assert visible == [weather_request, presentation_request]
    assert "action=turn_key_conflict" in caplog.text
    assert "kind=user_turn_key_content_conflict" in caplog.text


def test_user_without_turn_key_is_not_guessed_as_replay():
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "user", "content": "你好"},
    ]

    assert drop_non_display_messages(messages) == messages


def test_session_display_projection_hides_replay_before_history_render():
    from api.routes import _merged_session_messages_for_display

    class Session:
        session_id = "session-history"
        parent_session_id = None
        session_source = "webui"
        relationship_type = ""
        compression_recovery_source_session_id = ""
        compression_recovery_action = ""
        async_delegation_origins = {
            "deleg_123": {"turn_key": "turn:8", "status": "completed"},
        }
        messages = [
            {"role": "user", "content": "派发后台任务", "_turn_key": "turn:8"},
            {"role": "assistant", "content": "已派发", "_turn_key": "turn:8"},
            {"role": "user", "content": "派发后台任务", "_turn_key": "turn:8"},
            {"role": "assistant", "content": "后台任务完成", "_turn_key": "turn:8"},
        ]

    visible = _merged_session_messages_for_display(Session())

    assert [message["role"] for message in visible] == [
        "user", "assistant", "assistant",
    ]
    assert [message["content"] for message in visible] == [
        "派发后台任务", "已派发", "后台任务完成",
    ]
    assert "_background_task_ids" not in visible[0]


def test_verification_final_replaces_candidate_in_one_answer_projection():
    messages = [
        {"role": "user", "content": "修复登录超时", "_turn_key": "turn:1"},
        {
            "role": "assistant",
            "content": "登录接口已修复。",
            "finish_reason": "verification_required",
        },
        {
            "role": "user",
            "content": "运行测试",
            "_hermes_message_class": "internal_scaffold",
            "_hermes_scaffold_kind": "verification_stop",
        },
        {"role": "assistant", "content": "测试通过，登录接口已修复。"},
    ]

    visible = drop_non_display_messages(messages)

    assert [message["content"] for message in visible] == [
        "修复登录超时",
        "测试通过，登录接口已修复。",
    ]


def test_verification_candidate_is_the_fallback_when_no_final_exists():
    messages = [
        {"role": "user", "content": "修复登录超时", "_turn_key": "turn:1"},
        {
            "role": "assistant",
            "content": "登录接口已修复。",
            "finish_reason": "verification_required",
        },
        {
            "role": "user",
            "content": "运行测试",
            "_hermes_message_class": "internal_scaffold",
            "_hermes_scaffold_kind": "verification_stop",
        },
    ]

    visible = drop_non_display_messages(messages)

    assert [message["content"] for message in visible] == [
        "修复登录超时",
        "登录接口已修复。",
    ]


def test_empty_later_candidate_does_not_erase_non_empty_fallback():
    messages = [
        {"role": "user", "content": "修复登录超时", "_turn_key": "turn:1"},
        {
            "role": "assistant",
            "content": "登录接口已修复。",
            "finish_reason": "verification_required",
        },
        {
            "role": "user",
            "content": "运行测试",
            "_hermes_message_class": "internal_scaffold",
            "_hermes_scaffold_kind": "verification_stop",
        },
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "verification_required",
        },
        {
            "role": "user",
            "content": "再次运行测试",
            "_hermes_message_class": "internal_scaffold",
            "_hermes_scaffold_kind": "verification_stop",
        },
    ]

    visible = drop_non_display_messages(messages)

    assert [message["content"] for message in visible] == [
        "修复登录超时",
        "登录接口已修复。",
    ]


def test_codex_ack_is_replaced_but_tool_trace_is_preserved():
    messages = [
        {"role": "user", "content": "检查所有 TODO", "_turn_key": "turn:2"},
        {
            "role": "assistant",
            "content": "我接下来会搜索整个项目。",
            "finish_reason": "incomplete",
        },
        {
            "role": "user",
            "content": "立即调用工具",
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "intent_ack_continuation",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "search"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "找到 5 个 TODO"},
        {"role": "assistant", "content": "共找到 5 个 TODO。"},
    ]

    visible = drop_non_display_messages(messages)

    assert [message["role"] for message in visible] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [
        message["content"]
        for message in visible
        if message["role"] == "assistant" and not message.get("tool_calls")
    ] == ["共找到 5 个 TODO。"]


def test_length_continuation_merges_partial_and_does_not_mutate_source():
    messages = [
        {"role": "user", "content": "列出迁移步骤", "_turn_key": "turn:3"},
        {
            "role": "assistant",
            "content": "第一步创建新表；第二步启用双写；",
            "finish_reason": "length",
        },
        {
            "role": "user",
            "content": "从断点继续",
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "length_continuation",
        },
        {"role": "assistant", "content": "第三步回填数据；第四步灰度切流。"},
    ]
    source_before = copy.deepcopy(messages)

    visible = drop_non_display_messages(messages)

    assert [message["content"] for message in visible] == [
        "列出迁移步骤",
        "第一步创建新表；第二步启用双写；第三步回填数据；第四步灰度切流。",
    ]
    assert messages == source_before
