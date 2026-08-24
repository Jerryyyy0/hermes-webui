"""Fork-specific session-title language policy and prompt wording."""


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
