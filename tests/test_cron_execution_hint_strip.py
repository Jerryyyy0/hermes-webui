from api.streaming import _sanitize_cron_messages_for_display, _strip_cron_execution_hint

_CRON_HINT = (
    "[IMPORTANT: You are running as a scheduled cron job. "
    "DELIVERY: Your final response will be automatically delivered "
    "to the user — do NOT use send_message or try to deliver "
    "the output yourself. Just produce your report/output as your "
    "final response and the system handles the rest. "
    "SILENT: If there is genuinely nothing new to report, respond "
    'with exactly "[SILENT]" (nothing else) to suppress delivery. '
    "Never combine [SILENT] with content — either report your "
    "findings normally, or say [SILENT] and nothing more.]\n\n"
)

_USER_PROMPT = (
    '用友好、温暖的语气提醒用户去吃饭。直接说"该吃饭啦~"之类的话即可，保持简短自然。'
)


def test_strip_cron_execution_hint_removes_real_scheduler_prefix():
    raw = f"{_CRON_HINT}{_USER_PROMPT}"
    assert _strip_cron_execution_hint(raw) == _USER_PROMPT


def test_strip_cron_execution_hint_leaves_plain_user_message_unchanged():
    plain = "Check for updates and summarize findings."
    assert _strip_cron_execution_hint(plain) == plain


def test_strip_cron_execution_hint_does_not_strip_mid_body_silent_marker():
    text = "Report status. If nothing changed, reply with [SILENT] only."
    assert _strip_cron_execution_hint(text) == text


def test_strip_cron_execution_hint_handles_empty_and_none():
    assert _strip_cron_execution_hint("") == ""
    assert _strip_cron_execution_hint(None) == ""


def test_strip_cron_execution_hint_preserves_user_content_after_double_newline():
    user = "Generate a weekly digest."
    raw = f"{_CRON_HINT}{user}"
    assert _strip_cron_execution_hint(raw) == user


def test_strip_cron_execution_hint_does_not_strip_background_process_injection():
    background = (
        "[IMPORTANT: Background process proc_abc completed (exit code 0).\n"
        "Continue the user's prior request.]"
    )
    assert _strip_cron_execution_hint(background) == background


def test_sanitize_cron_messages_for_display_strips_user_rows_only():
    raw = f"{_CRON_HINT}{_USER_PROMPT}"
    messages = [
        {"role": "user", "content": raw, "timestamp": 1},
        {"role": "assistant", "content": "该下班啦", "timestamp": 2},
    ]
    sanitized = _sanitize_cron_messages_for_display(messages)
    assert sanitized[0]["content"] == _USER_PROMPT
    assert sanitized[0] is not messages[0]
    assert sanitized[1] is messages[1]


def test_sanitize_cron_messages_for_display_leaves_non_user_rows_unchanged():
    messages = [{"role": "assistant", "content": "[SILENT]"}]
    assert _sanitize_cron_messages_for_display(messages) == messages
