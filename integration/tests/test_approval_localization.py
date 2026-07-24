"""Regression coverage for Chinese WebUI approval display copy."""

from integration.approval_localization import localize_approval_payload
from integration.clarify_localization import clarify_language_rule


def test_pattern_description_is_localized_without_changing_keys():
    payload = {
        "description": "recursive delete; script execution via -e/-c flag",
        "pattern_key": "recursive delete",
        "pattern_keys": ["recursive delete", "script execution via -e/-c flag"],
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == "递归删除文件；通过 -e/-c 参数执行脚本"
    assert localized["description"] == payload["description"]
    assert localized["pattern_key"] == payload["pattern_key"]
    assert localized["pattern_keys"] == payload["pattern_keys"]


def test_pipe_to_interpreter_finding_is_localized_with_original_details():
    payload = {
        "description": (
            "Security scan — [HIGH] Pipe to interpreter: Command pipes output "
            "from 'curl' directly to interpreter 'python3'. Downloaded content "
            "will be executed without inspection.; script execution via -e/-c flag"
        ),
        "pattern_key": "tirith:pipe-to-interpreter",
        "pattern_keys": ["tirith:pipe-to-interpreter", "script execution via -e/-c flag"],
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == (
        "安全扫描：[高] 管道传入解释器：命令将“curl”的输出直接传给解释器“python3”执行，"
        "下载内容未经检查即会运行。；通过 -e/-c 参数执行脚本"
    )
    assert localized["pattern_keys"] == payload["pattern_keys"]


def test_unknown_tirith_finding_keeps_original_evidence():
    payload = {
        "description": "Security scan — [HIGH] Future rule: Untranslated evidence",
        "pattern_key": "tirith:future-rule",
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == "安全扫描：[高] Future rule: Untranslated evidence"


def test_clarify_rule_requires_simplified_chinese_without_translating_identifiers():
    rule = clarify_language_rule()

    assert "Simplified Chinese" in rule
    assert "commands, paths, URLs" in rule
