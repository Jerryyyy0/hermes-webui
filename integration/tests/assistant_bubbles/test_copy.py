from integration.assistant_bubbles import copy, generation


def test_normalize_emoji_punct_moves_tone_after_emoji():
    assert copy.normalize_emoji_punct("看详情哦~📝") == "看详情哦📝~"
    assert copy.normalize_emoji_punct("值守任务！💪") == "值守任务💪！"
    assert copy.normalize_emoji_punct("提神☕~") == "提神☕~"
    assert copy.normalize_emoji_punct("满格⚡！") == "满格⚡！"


def test_scheduled_task_text_puts_emoji_before_exclamation():
    text = copy.scheduled_task_text({"total": 2, "running": 0, "pending": 2, "failed": 0}, now=0)
    assert text.endswith("💪！")
    assert "！💪" not in text


def test_scheduled_task_emoji_rotates_across_windows():
    emojis = [
        copy.scheduled_task_emoji(now=i * copy.SCHEDULED_TASK_EMOJI_ROTATE_SECONDS)
        for i in range(len(copy.SCHEDULED_TASK_EMOJIS) * 2)
    ]
    assert emojis[: len(copy.SCHEDULED_TASK_EMOJIS)] == list(copy.SCHEDULED_TASK_EMOJIS)
    assert emojis[len(copy.SCHEDULED_TASK_EMOJIS) :] == list(copy.SCHEDULED_TASK_EMOJIS)


def test_scheduled_task_text_uses_rotated_emoji():
    stats = {"total": 2, "running": 0, "pending": 2, "failed": 0}
    first = copy.scheduled_task_text(stats, now=0)
    second = copy.scheduled_task_text(stats, now=copy.SCHEDULED_TASK_EMOJI_ROTATE_SECONDS)
    assert first.endswith(f"{copy.SCHEDULED_TASK_EMOJIS[0]}！")
    assert second.endswith(f"{copy.SCHEDULED_TASK_EMOJIS[1]}！")
    assert first != second


def test_validate_one_text_normalizes_trailing_emoji_punct():
    text, reason = generation.validate_one_text("memory", "看详情哦~📝")
    assert reason == "ok"
    assert text == "看详情哦📝~"
