"""Fork-specific session-title language policy and prompt wording."""

import re
from typing import Optional


_FIXED_CHINESE_TITLE_RULE = "标题必须使用简体中文。\n"


def build_title_prompts(user_text: str, assistant_text: str) -> tuple[str, list[str]]:
    """Build the Chinese title prompts used by both initial and refresh runs."""
    qa = f"用户问题：\n{user_text[:500]}\n\n助手回答：\n{assistant_text[:500]}"
    language_rule = title_language_rule(user_text)
    prompts = [
        (
            "请根据这段对话开头生成一个简短的会话标题。\n"
            "请同时参考用户的问题和助手已经展示的回答。\n"
            f"{language_rule}"
            "只输出标题文本，长度约为 3-8 个词语，并将其作为主题标签。\n"
            "不要使用 Markdown、项目符号、标签或类似“会话标题：”的前缀。\n"
            "不要输出完整句子。\n"
            "不要输出“好的”“已完成”“没问题”等确认或完成性话语。\n"
            "不要描述内部推理过程。\n"
            "错误示例：用户正在询问……、好的、已完成。\n"
            "正确示例：标题生成测试、澄清对话布局、GitHub 问题分类"
        ),
        (
            "请将这段对话开头改写为简洁的名词性短语标题。\n"
            "请概括实际讨论主题，不要描述任务结果。\n"
            f"{language_rule}"
            "只输出标题文本。\n"
            "不要使用 Markdown、项目符号、标签或类似“会话标题：”的前缀。\n"
            "不要输出确认语、完成状态或元话语。"
        ),
    ]
    return qa, prompts


def title_language_rule(_: str) -> str:
    """Return the fixed language instruction for every WebUI title request."""
    return _FIXED_CHINESE_TITLE_RULE


def should_validate_source_language_match() -> bool:
    """WebUI titles are intentionally independent of the source message language."""
    return False


def fallback_title_from_exchange(user_text: str, assistant_text: str) -> Optional[str]:
    """Build a Chinese local title when both model title attempts fail.

    This is deliberately deterministic: it must remain useful when the title
    request fails because of a provider error or rate limit.  User-provided
    topic fragments are retained where possible, while all generated labels
    and suffixes stay in Chinese.
    """
    raw_user_text = str(user_text or "").strip()
    raw_assistant_text = str(assistant_text or "").strip()
    raw_combined = f"{raw_user_text} {raw_assistant_text}".strip()
    # Classify attachments before removing their paths from title text.
    if _mentions_pdf_conversion(raw_combined):
        return "文档转 PDF"
    if _mentions_image(raw_combined):
        return "图片内容分析"

    user_text = _clean_title_source(raw_user_text)
    assistant_text = _clean_title_source(raw_assistant_text)
    if not user_text:
        return None

    combined_raw = f"{user_text} {assistant_text}".strip()
    combined = combined_raw.lower()

    topic_name = _extract_named_topic(combined_raw)
    if topic_name:
        if _mentions_time_management(combined):
            return _with_chinese_suffix(topic_name, "时间管理")
        if any(k in combined for k in ("hermes", "codex", "ai", "人工智能")):
            return _with_chinese_suffix(topic_name, "AI 效率")
        return _with_chinese_suffix(topic_name, "讨论")

    if _mentions_title_summary(combined):
        return "会话标题自动摘要测试" if any(
            k in combined for k in ("test", "测试", "ok", "reply ok")
        ) else "会话标题自动摘要"
    if any(k in combined for k in ("clarify", "clarification", "澄清")) and any(
        k in combined for k in ("dialog", "card", "对话", "卡片")
    ):
        return "澄清对话卡片"
    if any(k in combined for k in ("issue", "github", "pr", "问题")) and any(
        k in combined for k in ("triage", "bug", "review", "分类", "评审")
    ):
        return "GitHub 问题分类"

    chinese_fragment = _extract_chinese_fragment(user_text)
    if chinese_fragment:
        return f"{chinese_fragment}讨论"[:60]

    # Preserve a short user-provided technical/topic fragment, but make the
    # fallback visibly Chinese instead of returning an English-only label.
    latin_fragment = _extract_latin_fragment(user_text)
    if latin_fragment:
        return f"{latin_fragment} 讨论"[:60]
    return "会话主题"


def _clean_title_source(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s*\[(?:Attached files|Attached files for this steer):.*$", "", value, flags=re.IGNORECASE | re.DOTALL)
    # Avoid turning an uploaded absolute path into the title. Keep the file
    # extension for image/PDF classification before this helper is called.
    value = re.sub(r"(?:/|\\)[^\s]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_named_topic(text: str) -> str:
    for pattern in (r'"([^"\n]{2,24})"', r"“([^”\n]{2,24})”"):
        match = re.search(pattern, text)
        if match:
            return (match.group(1) or "").strip()
    return ""


def _mentions_pdf_conversion(text: str) -> bool:
    # Chinese characters are word characters to Python's regex engine, so
    # ASCII word boundaries do not match ``转为pdf``. Use ASCII-only guards.
    has_pdf = bool(re.search(r"(?<![A-Za-z0-9_])pdf(?![A-Za-z0-9_])", text, re.IGNORECASE))
    convert_words = ("转", "转换", "转为", "convert", "export", "导出", "生成")
    return has_pdf and any(word in text for word in convert_words)


def _mentions_image(text: str) -> bool:
    has_file_extension = bool(re.search(r"\.(?:png|jpe?g|gif|webp)(?:\b|$)", text, re.IGNORECASE))
    has_image = has_file_extension or bool(re.search(r"(?:\b(?:image|images|bilder)\b|图片|图像|照片|截图)", text, re.IGNORECASE))
    image_action = ("分析", "识别", "查看", "显示", "内容", "analy", "display", "show", "render", "anzeigen")
    return has_file_extension or (has_image and any(word in text for word in image_action)) or bool(re.search(r"图片|图像|照片|截图|bilder", text, re.IGNORECASE))


def _mentions_time_management(text: str) -> bool:
    return any(k in text for k in ("time", "schedule", "efficiency", "manage", "fitness", "singing", "calligraphy", "时间", "日程", "效率"))


def _mentions_title_summary(text: str) -> bool:
    return any(k in text for k in ("title", "session title", "标题", "会话")) and any(
        k in text for k in ("summary", "summar", "short title", "摘要", "简短")
    )


def _with_chinese_suffix(topic: str, suffix: str) -> str:
    return f"{topic.strip()} {suffix}"[:60]


def _extract_chinese_fragment(text: str) -> str:
    if not any(k in text for k in ("报错", "失败", "问题", "标题", "对话", "布局", "代码", "文件", "模型", "配置", "会话", "生成", "修复")):
        return ""
    fragments = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", text)
    stop = {"请帮我", "帮我", "请问", "请", "一下", "这个问题", "的问题", "讨论一下"}
    for fragment in fragments:
        value = fragment
        for prefix in sorted(stop, key=len, reverse=True):
            if value.startswith(prefix):
                value = value[len(prefix):]
        value = value.strip("，。！？：、；")
        if value and value not in stop:
            return value[:24]
    return ""


def _extract_latin_fragment(text: str) -> str:
    stop = {
        "the", "this", "that", "with", "from", "into", "just", "reply", "please",
        "need", "needs", "want", "wants", "user", "assistant", "could", "would",
        "should", "about", "there", "here", "test", "testing", "title", "summary",
    }
    latin_word = r"A-Za-z0-9À-ÖØ-öø-ÿ"
    head = re.split(r"[.!?\n]", text)[0].strip()
    tokens = re.findall(rf"[{latin_word}][{latin_word}_./+-]*", head)
    picked = []
    for token in tokens:
        if token.lower() in stop or len(token) < 3 or token in picked:
            continue
        picked.append(token)
        if len(picked) >= 4:
            break
    return " ".join(picked)[:48]
