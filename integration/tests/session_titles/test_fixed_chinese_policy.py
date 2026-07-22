"""Regression coverage for the fork's fixed Chinese WebUI title policy."""

from unittest.mock import patch


def test_title_prompts_always_require_simplified_chinese():
    from api.streaming import _title_prompts

    _, prompts = _title_prompts(
        "I've uploaded 1 file(s): presentation.pptx",
        "I will inspect the presentation.",
    )

    assert all("Write the title in Simplified Chinese." in prompt for prompt in prompts)
    assert all("Match the language of the user question" not in prompt for prompt in prompts)


def test_aux_title_accepts_chinese_for_english_conversation_start():
    from api.streaming import _generate_llm_session_title_via_aux

    with patch(
        "api.streaming.generate_title_raw_via_aux",
        return_value=("人工智能效率讨论", "llm_aux"),
    ):
        title, status, raw_preview = _generate_llm_session_title_via_aux(
            "I've uploaded 1 file(s): presentation.pptx",
            "I will inspect the presentation.",
        )

    assert title == "人工智能效率讨论"
    assert status == "llm_aux"
    assert raw_preview == ""


def test_aux_title_does_not_reject_an_unexpected_model_language():
    from api.streaming import _generate_llm_session_title_via_aux

    with patch(
        "api.streaming.generate_title_raw_via_aux",
        return_value=("AI Productivity Discussion", "llm_aux"),
    ):
        title, status, raw_preview = _generate_llm_session_title_via_aux(
            "请帮我总结这个演示文稿",
            "我会先提取内容。",
        )

    assert title == "AI Productivity Discussion"
    assert status == "llm_aux"
    assert raw_preview == ""