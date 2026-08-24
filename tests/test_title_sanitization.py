import unittest

from api.streaming import (
    _fallback_title_from_exchange,
    _first_exchange_snippets,
    _sanitize_generated_title,
)


class TestGeneratedTitleSanitization(unittest.TestCase):
    def test_strips_session_title_markdown_prefix(self):
        self.assertEqual(
            _sanitize_generated_title("**Session Title:** Clarifying Topic for Discussion"),
            "Clarifying Topic for Discussion",
        )

    def test_strips_plain_title_prefix(self):
        self.assertEqual(
            _sanitize_generated_title("Title: Clarifying Topic for Discussion"),
            "Clarifying Topic for Discussion",
        )

    def test_strips_wrapping_markdown_emphasis(self):
        self.assertEqual(
            _sanitize_generated_title("**Clarifying Topic for Discussion**"),
            "Clarifying Topic for Discussion",
        )

    def test_first_exchange_skips_empty_assistant_tool_call_placeholder(self):
        messages = [
            {"role": "user", "content": "What time is it in San Francisco?"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "tool output", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "It is 6:16 PM in San Francisco."},
        ]
        self.assertEqual(
            _first_exchange_snippets(messages),
            ("What time is it in San Francisco?", "It is 6:16 PM in San Francisco."),
        )

    def test_fallback_title_uses_chinese_discussion_suffix(self):
        self.assertEqual(
            _fallback_title_from_exchange('Please review "random cancel"', ""),
            "random cancel 讨论",
        )

    def test_fallback_title_summary_label_is_chinese(self):
        self.assertEqual(
            _fallback_title_from_exchange("Generate a short title summary test", ""),
            "会话标题自动摘要测试",
        )

    def test_fallback_title_non_latin_input_uses_chinese_topic(self):
        self.assertEqual(
            _fallback_title_from_exchange("讨论一下这个问题", ""),
            "会话主题",
        )

    def test_fallback_title_non_latin_quoted_topic_uses_chinese_suffix(self):
        self.assertEqual(
            _fallback_title_from_exchange('Please review "讨论主题"', ""),
            "讨论主题 讨论",
        )

    def test_fallback_title_handles_pdf_conversion_without_attachment_path(self):
        self.assertEqual(
            _fallback_title_from_exchange(
                "帮我转为pdf\n\n[Attached files: /Users/wzq/Documents/report.docx]",
                "",
            ),
            "文档转 PDF",
        )

    def test_fallback_title_handles_image_analysis_without_attachment_path(self):
        self.assertEqual(
            _fallback_title_from_exchange(
                "我已上传 1 个文件\n\n[Attached files: /Users/wzq/image.png]",
                "",
            ),
            "图片内容分析",
        )

    def test_title_prompts_are_chinese(self):
        from api.streaming import _title_prompts

        _, prompts = _title_prompts("Summarize this title routing bug.", "The title route needs a fix.")
        self.assertTrue(all("标题必须使用简体中文" in prompt for prompt in prompts))
        self.assertTrue(all("Generate a short session title" not in prompt for prompt in prompts))
