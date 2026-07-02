"""Tests for Chinese interrupted-turn recovery marker copy."""
from integration.chat_provider_errors.interruption_copy import (
    INTERRUPTED_NEUTRAL_ZH,
    INTERRUPTED_NO_OUTPUT_ZH,
    INTERRUPTED_PENDING_RETRY_ZH,
    INTERRUPTED_RECOVERED_ZH,
    build_interrupted_content_zh,
)


def test_marker_constants_use_chinese_lead():
    for text in (
        INTERRUPTED_RECOVERED_ZH,
        INTERRUPTED_NO_OUTPUT_ZH,
        INTERRUPTED_PENDING_RETRY_ZH,
        INTERRUPTED_NEUTRAL_ZH,
    ):
        assert "响应已中断" in text
        assert "完成前中断" in text
        assert "Response interrupted" not in text


def test_build_interrupted_content_zh_process_restart():
    content = build_interrupted_content_zh(
        recovered_output=False,
        pending_retry=False,
        interruption_cause="process_restart",
    )
    assert "WebUI 进程在本轮开始之后才启动" in content
    assert "上方的用户消息已保留" in content


def test_build_interrupted_content_zh_recovered_output():
    content = build_interrupted_content_zh(
        recovered_output=True,
        pending_retry=False,
        interruption_cause="unknown",
    )
    assert "上方部分输出已从运行日志恢复" in content
