"""Build Chinese-only display copy without changing approval protocol keys."""

from __future__ import annotations

import re
from collections.abc import Mapping

_REASON_TEXT = {
    "delete in root path": "删除根目录中的内容",
    "recursive delete": "递归删除文件",
    "recursive delete (long flag)": "递归删除文件（长参数）",
    "format filesystem": "格式化文件系统",
    "disk copy": "直接复制磁盘数据",
    "write to block device": "写入块设备",
    "SQL DROP": "删除 SQL 表或数据库",
    "SQL DELETE without WHERE": "未带 WHERE 条件的 SQL DELETE",
    "SQL TRUNCATE": "清空 SQL 表",
    "overwrite system config": "覆盖系统配置",
    "stop/restart system service": "停止或重启系统服务",
    "kill all processes": "终止全部进程",
    "force kill processes": "强制终止进程",
    "fork bomb": "fork 炸弹",
    "pipe remote content to shell": "将远程内容直接传给 shell 执行",
    "execute remote script via process substitution": "通过进程替换执行远程脚本",
    "execute remote content via command substitution": "通过命令替换执行远程内容",
    "pipe decoded content to shell (possible command obfuscation)": "将解码内容传给 shell 执行（可能存在命令混淆）",
    "pipe xxd-decoded content to shell (possible command obfuscation)": "将 xxd 解码内容传给 shell 执行（可能存在命令混淆）",
    "pipe tr-transformed output to shell (possible command obfuscation)": "将 tr 转换后的内容传给 shell 执行（可能存在命令混淆）",
    "pipe openssl-decoded content to shell (possible command obfuscation)": "将 OpenSSL 解码内容传给 shell 执行（可能存在命令混淆）",
    "overwrite system file via tee": "通过 tee 覆盖系统文件",
    "overwrite system file via redirection": "通过重定向覆盖系统文件",
    "overwrite project env/config via tee": "通过 tee 覆盖项目环境或配置",
    "overwrite project env/config via redirection": "通过重定向覆盖项目环境或配置",
    "xargs with rm": "通过 xargs 调用 rm",
    "find -exec/-execdir rm": "通过 find -exec 执行 rm",
    "find -delete": "通过 find 删除文件",
    "shell execution via heredoc": "通过 heredoc 执行 shell 命令",
    "script execution via -e/-c flag": "通过 -e/-c 参数执行脚本",
    "script execution via heredoc": "通过 heredoc 执行脚本",
    "shell command via -c/-lc flag": "通过 -c/-lc 参数执行 shell 命令",
    "command parser limit exceeded": "命令解析器长度超过限制",
    "command parser limit or malformed executable payload": "命令解析器达到限制或可执行载荷格式错误",
    "Tirith security module unavailable": "Tirith 安全模块不可用",
    "execute_code": "execute_code 脚本执行。该脚本可能创建子进程或修改文件，且不会经过终端命令审批；本次批准仅对当前这次运行有效。",
}

_EXECUTE_CODE_DESCRIPTION = (
    "execute_code script execution. The script can spawn subprocesses or "
    "mutate files without passing through terminal command approval; "
    "approval is one-shot for this run."
)
_EXECUTE_CODE_DESCRIPTION_ZH = _REASON_TEXT["execute_code"]

_SEVERITY_TEXT = {
    "CRITICAL": "严重",
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
    "INFO": "提示",
}

_TIRITH_RULE_TEXT = {
    "pipe-to-interpreter": "管道传入解释器",
    "pipe_to_interpreter": "管道传入解释器",
    "schemeless-url-in-sink-context": "在执行上下文中使用无协议 URL",
    "schemeless_url_in_sink_context": "在执行上下文中使用无协议 URL",
    "variation-selector-characters": "检测到 Unicode 变体选择符",
    "variation_selector_characters": "检测到 Unicode 变体选择符",
    "variation-selectors": "检测到 Unicode 变体选择符",
    "variation_selectors": "检测到 Unicode 变体选择符",
    "unicode-variation-selectors": "检测到 Unicode 变体选择符",
    "unicode_variation_selectors": "检测到 Unicode 变体选择符",
}

_TIRITH_TITLE_TEXT = {
    "Pipe to interpreter": "管道传入解释器",
    "Schemeless URL in sink context": "在执行上下文中使用无协议 URL",
    "Variation selector characters detected": "检测到 Unicode 变体选择符",
}

_TIRITH_DESCRIPTION_TEXT = {
    (
        "Content contains Unicode variation selectors (VS1-256). These are commonly "
        "used in emoji sequences but may indicate steganographic encoding or obfuscation"
    ): (
        "内容包含 Unicode 变体选择符（VS1-256）。它们常见于 emoji 序列，"
        "但也可能用于隐写编码或混淆。"
    ),
    (
        "URL without explicit scheme passed to a command that downloads/executes content"
    ): "无协议 URL 被传给会下载或执行内容的命令，可能造成来源识别或执行风险。",
}

_PIPE_TO_INTERPRETER_RE = re.compile(
    r"^Command pipes output from '([^']+)' directly to interpreter '([^']+)'\. "
    r"Downloaded content will be executed without inspection\."
)
_TIRITH_FINDING_PROSE_RE = re.compile(
    r"^\[(?P<severity>[A-Z]+)\] (?P<title>[^:;]+)(?:: (?P<detail>.*))?$"
)


def _localize_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    if not reason:
        return "检测到安全风险"
    if reason in _REASON_TEXT:
        return _REASON_TEXT[reason]
    match = re.fullmatch(r"arbitrary program execution via (.+)", reason)
    if match:
        return f"通过 {match.group(1)} 执行任意程序"
    return reason


def _localize_tirith_detail(rule_id: str, title: str, description: str) -> str:
    if rule_id in {"pipe-to-interpreter", "pipe_to_interpreter"} or title == "Pipe to interpreter":
        pipe_match = _PIPE_TO_INTERPRETER_RE.match(description)
        if pipe_match:
            producer, interpreter = pipe_match.groups()
            return (
                f"命令将“{producer}”的输出直接传给解释器“{interpreter}”执行，"
                "下载内容未经检查即会运行。"
            )
        if description:
            return "命令将输出直接传给解释器执行，下载内容未经检查即会运行。"
    if rule_id in {
        "schemeless-url-in-sink-context",
        "schemeless_url_in_sink_context",
    } or title == "Schemeless URL in sink context":
        return "无协议 URL 被传给会下载或执行内容的命令，可能造成来源识别或执行风险。"
    if description in _TIRITH_DESCRIPTION_TEXT:
        return _TIRITH_DESCRIPTION_TEXT[description]
    return description


def _localize_tirith_finding(finding: Mapping[str, object]) -> str:
    severity = str(finding.get("severity") or "").upper()
    rule_id = str(finding.get("rule_id") or "")
    title = str(finding.get("title") or "").strip()
    description = str(finding.get("description") or "").strip()
    localized_title = (
        _TIRITH_RULE_TEXT.get(rule_id)
        or _TIRITH_TITLE_TEXT.get(title)
    )
    localized_description = _localize_tirith_detail(rule_id, title, description)

    prefix = f"[{_SEVERITY_TEXT.get(severity, severity)}] " if severity else ""
    if localized_title:
        return f"{prefix}{localized_title}" + (f"：{localized_description}" if localized_description else "")
    if title and description:
        return f"{prefix}{title}：{description}"
    return f"{prefix}{title or description}".strip()


def _localize_tirith_prose_finding(segment: str) -> str:
    match = _TIRITH_FINDING_PROSE_RE.match(segment.strip())
    if not match:
        return _localize_reason(segment)
    severity = match.group("severity")
    title = (match.group("title") or "").strip()
    detail = (match.group("detail") or "").strip()
    return _localize_tirith_finding(
        {
            "severity": severity,
            "title": title,
            "description": detail,
            "rule_id": "",
        }
    )


def _localize_tirith_description(description: str) -> str:
    body = description.removeprefix("Security scan — ").removeprefix("Security scan: ")
    if not body:
        return "安全扫描：检测到安全风险"
    if body == "security issue detected":
        return "安全扫描：检测到安全风险"
    if body == "security warning detected (details unavailable)":
        return "安全扫描：检测到安全警告（详情不可用）"
    if body.startswith("tirith timed out"):
        return "安全扫描：Tirith 扫描超时"
    if body.startswith("tirith unavailable"):
        return "安全扫描：Tirith 不可用"

    # Prefer parsing "[SEVERITY] Title: detail" segments so known Tirith titles
    # localize even when the Agent has not yet attached tirith_findings.
    segments = [part.strip() for part in body.split("; ") if part.strip()]
    if any(_TIRITH_FINDING_PROSE_RE.match(segment) for segment in segments):
        return "安全扫描：" + "；".join(_localize_tirith_prose_finding(segment) for segment in segments)

    for source, translated in _SEVERITY_TEXT.items():
        body = body.replace(f"[{source}]", f"[{translated}]")
    return f"安全扫描：{body}"


def _localize_reason_list(description: str) -> str:
    reasons = [part.strip() for part in str(description or "").split(";") if part.strip()]
    return "；".join(_localize_reason(reason) for reason in reasons)


def localize_approval_payload(approval: Mapping[str, object]) -> dict:
    """Return a payload with Chinese display copy and untouched approval keys.

    `description`, `pattern_key`, and `pattern_keys` are canonical Agent data.
    They remain untouched for approval resolution, allowlists, Smart Approval,
    plugins, and non-WebUI clients. The browser consumes only the added display
    field when it is present.
    """
    payload = dict(approval)
    description = str(payload.get("description") or "").strip()
    pattern_key = str(payload.get("pattern_key") or "").strip()
    if pattern_key == "execute_code" or description == _EXECUTE_CODE_DESCRIPTION:
        payload["display_description_zh"] = _EXECUTE_CODE_DESCRIPTION_ZH
        return payload
    if description.startswith("Security scan"):
        findings = payload.get("tirith_findings")
        if isinstance(findings, list):
            parts = [
                _localize_tirith_finding(item)
                for item in findings
                if isinstance(item, Mapping)
            ]
            if parts:
                pattern_parts = [
                    _localize_reason(str(key))
                    for key in (payload.get("pattern_keys") or [])
                    if key and not str(key).startswith("tirith:")
                ]
                payload["display_description_zh"] = "安全扫描：" + "；".join(parts + pattern_parts)
                return payload
        payload["display_description_zh"] = _localize_tirith_description(description)
    else:
        payload["display_description_zh"] = _localize_reason_list(description)
    return payload
