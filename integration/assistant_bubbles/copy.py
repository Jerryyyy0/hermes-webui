"""Deterministic assistant bubble copy."""

from __future__ import annotations

import re
import time
from typing import Any

# Trailing tone punctuation that should follow (not precede) a bubble emoji.
_TONE_PUNCT = r"[~！？!?～]+"
# Common emoji / pictograph run used at bubble endings (single glyph + optional VS16/ZWJ).
_EMOJI_RUN = (
    r"(?:"
    r"[\U0001F300-\U0001FAFF]"
    r"|[\U00002600-\U000027BF]"
    r"|[\U0001F1E6-\U0001F1FF]{2}"
    r")"
    r"(?:\uFE0F|\u200D(?:[\U0001F300-\U0001FAFF]|[\U00002600-\U000027BF])\uFE0F?)*"
)
_EMOJI_BEFORE_TONE = re.compile(rf"({_TONE_PUNCT})({_EMOJI_RUN})$")

# Duty / watch-style emojis; rotate on each scheduled_task render window.
SCHEDULED_TASK_EMOJIS = ("💪", "⏰", "📌", "🫡")
SCHEDULED_TASK_EMOJI_ROTATE_SECONDS = 60


def normalize_emoji_punct(text: str) -> str:
    """Put trailing emoji before trailing tone punctuation: '哦~📝' -> '哦📝~'."""
    value = str(text or "")
    while True:
        updated = _EMOJI_BEFORE_TONE.sub(r"\2\1", value)
        if updated == value:
            return value
        value = updated


def limit_text(text: str, max_chars: int = 50) -> str:
    text = normalize_emoji_punct(" ".join(str(text or "").split()))
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def fallback_text(category: str, context: dict[str, Any] | None = None) -> str:
    context = context or {}
    if category == "assistant_intro":
        name = str(context.get("display_name") or "Hermes").strip() or "Hermes"
        desc = str(context.get("description") or "协助处理日常工作").strip() or "协助处理日常工作"
        return limit_text(f"哈喽，我是{name}，可以帮你{desc}。")
    if category == "memory":
        if context.get("latest_memory"):
            return "我记录了新的工作记忆，点我可查看详情。"
        return "我会在有新记忆时提醒你查看。"
    if category == "skill":
        count = int(context.get("skills_count") or 0)
        if count > 0:
            return limit_text(f"我已准备好{count}项技能，随时协助你推进工作。")
        return "我会在有可用技能时告诉你。"
    if category == "emotion":
        return "我在这里，随时陪你推进下一步😊"
    return "我已准备好协助你。"


def fallback_emotions(context: dict[str, Any] | None = None) -> list[str]:
    name = str((context or {}).get("display_name") or "我").strip() or "我"
    values = [
        "我在这里，随时陪你推进下一步😊",
        "今天也想帮你把事情理顺💪",
        "我会保持专注，陪你稳稳处理任务✨",
        f"有需要时，点{name}就能继续聊😄",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = limit_text(value)
        if text not in seen:
            out.append(text)
            seen.add(text)
    while len(out) < 4:
        out.append("我会安静等你，需要时随时回应😊")
    return out[:4]


def scheduled_task_emoji(now: float | None = None) -> str:
    """Round-robin emoji for scheduled_task bubbles (stable within each rotate window)."""
    ts = time.time() if now is None else float(now)
    slot = int(ts // SCHEDULED_TASK_EMOJI_ROTATE_SECONDS)
    return SCHEDULED_TASK_EMOJIS[slot % len(SCHEDULED_TASK_EMOJIS)]


def scheduled_task_text(stats: dict[str, int], *, now: float | None = None) -> str:
    total = int(stats.get("total") or 0)
    running = int(stats.get("running") or 0)
    pending = int(stats.get("pending") or 0)
    failed = int(stats.get("failed") or 0)
    emoji = scheduled_task_emoji(now)
    full = f"我当前负责{total}个定时任务，包括执行中{running}个、待执行{pending}个、执行异常{failed}个，我会持续稳定值守任务{emoji}！"
    if len(full) <= 50:
        return full
    short = f"我负责{total}个定时任务：执行中{running}个、待执行{pending}个、异常{failed}个{emoji}！"
    if len(short) <= 50:
        return short
    no_emoji = re.sub(rf"{re.escape(emoji)}！?$", "", short)
    return limit_text(no_emoji)
