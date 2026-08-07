"""Display projection regressions for replayed Agent user rows."""

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

    with caplog.at_level("INFO"):
        visible = drop_non_display_messages(
            [canonical, replay],
            session_id="session-1",
        )

    assert visible == [{
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
        "_background_task_ids": ["deleg_123"],
    }]
    assert canonical == {
        "role": "user",
        "content": "你好",
        "_turn_key": "turn:8",
    }
    assert "action=display_duplicate_drop" in caplog.text
    assert "kind=async_origin_user_replay" in caplog.text
    assert "content='你好'" in caplog.text


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

    with caplog.at_level("INFO"):
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
    assert visible[0]["_background_task_ids"] == ["deleg_123"]
