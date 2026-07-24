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


def test_execute_code_description_is_localized_without_changing_key():
    payload = {
        "description": (
            "execute_code script execution. The script can spawn subprocesses or "
            "mutate files without passing through terminal command approval; "
            "approval is one-shot for this run."
        ),
        "pattern_key": "execute_code",
        "pattern_keys": ["execute_code"],
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == (
        "execute_code 脚本执行。该脚本可能创建子进程或修改文件，且不会经过终端命令审批；"
        "本次批准仅对当前这次运行有效。"
    )
    assert localized["description"] == payload["description"]
    assert localized["pattern_key"] == "execute_code"


def test_structured_tirith_findings_are_localized_independently():
    payload = {
        "description": "Security scan — [MEDIUM] ...; [HIGH] ...",
        "pattern_key": "tirith:schemeless-url-in-sink-context",
        "pattern_keys": [
            "tirith:schemeless-url-in-sink-context",
            "script execution via -e/-c flag",
        ],
        "tirith_findings": [
            {
                "rule_id": "schemeless-url-in-sink-context",
                "severity": "MEDIUM",
                "title": "Schemeless URL in sink context",
                "description": "URL without explicit scheme passed to a command",
            },
            {
                "rule_id": "pipe-to-interpreter",
                "severity": "HIGH",
                "title": "Pipe to interpreter",
                "description": (
                    "Command pipes output from 'curl' directly to interpreter 'python3'. "
                    "Downloaded content will be executed without inspection."
                ),
            },
        ],
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == (
        "安全扫描：[中] 在执行上下文中使用无协议 URL：无协议 URL 被传给会下载或执行内容的命令，"
        "可能造成来源识别或执行风险。；[高] 管道传入解释器：命令将“curl”的输出直接传给"
        "解释器“python3”执行，下载内容未经检查即会运行。；通过 -e/-c 参数执行脚本"
    )
    assert localized["description"] == payload["description"]
    assert localized["pattern_keys"] == payload["pattern_keys"]


def test_unknown_structured_tirith_finding_keeps_original_evidence():
    payload = {
        "description": "Security scan — [HIGH] Future rule: Untranslated evidence",
        "pattern_key": "tirith:future-rule",
        "tirith_findings": [
            {
                "rule_id": "future-rule",
                "severity": "HIGH",
                "title": "Future rule",
                "description": "Untranslated evidence",
            }
        ],
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == "安全扫描：[高] Future rule：Untranslated evidence"


def test_legacy_prose_tirith_payload_still_localizes_severity():
    payload = {
        "description": "Security scan — [HIGH] Future rule: Untranslated evidence",
        "pattern_key": "tirith:future-rule",
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == "安全扫描：[高] Future rule：Untranslated evidence"


def test_variation_selector_prose_is_localized():
    payload = {
        "description": (
            "Security scan — [MEDIUM] Variation selector characters detected: "
            "Content contains Unicode variation selectors (VS1-256). These are commonly "
            "used in emoji sequences but may indicate steganographic encoding or obfuscation"
        ),
        "pattern_key": "tirith:variation-selectors",
    }

    localized = localize_approval_payload(payload)

    assert localized["display_description_zh"] == (
        "安全扫描：[中] 检测到 Unicode 变体选择符：内容包含 Unicode 变体选择符（VS1-256）。"
        "它们常见于 emoji 序列，但也可能用于隐写编码或混淆。"
    )


def test_clarify_rule_requires_simplified_chinese_without_translating_identifiers():
    rule = clarify_language_rule()

    assert "Simplified Chinese" in rule
    assert "commands, paths, URLs" in rule
