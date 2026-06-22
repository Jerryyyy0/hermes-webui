from api.routes import _message_window_for_display, _turn_aligned_window_indices


def _build_turn(user_content, assistant_content, tool_count=0):
    messages = [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": assistant_content},
    ]
    for idx in range(tool_count):
        messages.append({"role": "tool", "content": f"tool {idx}"})
    return messages


def test_last_turn_exceeds_limit_returns_whole_turn():
    messages = _build_turn("question", "answer", tool_count=58)

    start_idx, end_idx = _turn_aligned_window_indices(messages, 50)
    window, offset = _message_window_for_display(messages, msg_limit=50, turn_align=True)

    assert start_idx == 0
    assert end_idx == len(messages)
    assert len(window) == 60
    assert offset == 0
    assert window[0]["role"] == "user"
    assert window[-1]["role"] == "tool"


def test_multiple_turns_accumulate_within_limit():
    turn_a = _build_turn("u1", "a1", tool_count=30)
    turn_b = _build_turn("u2", "a2", tool_count=25)
    turn_c = _build_turn("u3", "a3", tool_count=20)
    messages = turn_a + turn_b + turn_c

    window, offset = _message_window_for_display(messages, msg_limit=50, turn_align=True)

    assert offset == len(turn_a)
    assert len(window) == len(turn_b) + len(turn_c)
    assert window[0]["content"] == "u2"
    assert window[-1]["content"] == "tool 19"


def test_turn_align_does_not_cut_mid_turn():
    messages = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "t1"},
        {"role": "tool", "content": "t2"},
    ]

    raw_window, _ = _message_window_for_display(messages, msg_limit=3, turn_align=False)
    turn_window, _ = _message_window_for_display(messages, msg_limit=3, turn_align=True)

    assert [m["content"] for m in raw_window] == ["a1", "t1", "t2"]
    assert [m["content"] for m in turn_window] == ["u1", "a1", "t1", "t2"]


def test_msg_before_with_turn_align():
    turn_a = _build_turn("older", "older-a", tool_count=2)
    turn_b = _build_turn("newer", "newer-a", tool_count=2)
    messages = turn_a + turn_b
    before_idx = len(turn_a)

    window, offset = _message_window_for_display(
        messages,
        msg_limit=50,
        msg_before=before_idx,
        turn_align=True,
    )

    assert offset == 0
    assert [m["content"] for m in window] == ["older", "older-a", "tool 0", "tool 1"]


def test_expand_renderable_ignored_when_turn_align():
    messages = [
        ({"role": "user", "content": f"u{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"})
        for i in range(10)
    ] + [
        {"role": "tool", "content": f"tool {idx}"}
        for idx in range(10, 14)
    ]

    expanded_window, expanded_offset = _message_window_for_display(
        messages,
        msg_limit=5,
        expand_renderable=True,
        turn_align=False,
    )
    turn_window, turn_offset = _message_window_for_display(
        messages,
        msg_limit=5,
        expand_renderable=True,
        turn_align=True,
    )

    assert expanded_offset == 5
    assert turn_offset == 8
    assert [m["content"] for m in turn_window if m["role"] != "tool"] == ["u8", "a9"]
    assert [m["content"] for m in expanded_window if m["role"] != "tool"] == [
        "a5", "u6", "a7", "u8", "a9",
    ]


def test_tool_only_session_falls_back_to_raw_tail():
    messages = [{"role": "tool", "content": f"tool {idx}"} for idx in range(6)]

    window, offset = _message_window_for_display(messages, msg_limit=3, turn_align=True)

    assert offset == 3
    assert [m["content"] for m in window] == ["tool 3", "tool 4", "tool 5"]


def test_turn_align_returns_full_last_turn_including_tools():
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ] + [
        {"role": "tool", "content": f"tool result {idx}"}
        for idx in range(40)
    ]

    window, offset = _message_window_for_display(messages, msg_limit=50, turn_align=True)

    assert offset == 0
    assert len(window) == 42
    assert window[0]["role"] == "user"
    assert window[1]["role"] == "assistant"
    assert window[-1]["role"] == "tool"


def test_truncation_flag_when_earlier_turns_exist():
    turn_a = _build_turn("u1", "a1", tool_count=30)
    turn_b = _build_turn("u2", "a2", tool_count=5)
    messages = turn_a + turn_b

    window, offset = _message_window_for_display(messages, msg_limit=10, turn_align=True)

    assert offset > 0
    assert window[0]["content"] == "u2"
    assert len(window) == len(turn_b)
