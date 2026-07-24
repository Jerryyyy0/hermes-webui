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
}

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
}

_TIRITH_TITLE_TEXT = {
    "Pipe to interpreter": "管道传入解释器",
}

_PIPE_TO_INTERPRETER_RE = re.compile(
    r"^Command pipes output from '([^']+)' directly to interpreter '([^']+)'\. "
    r"Downloaded content will be executed without inspection\."
)
_TIRITH_PIPE_DESCRIPTION_RE = re.compile(
    r"^\[(?P<severity>[A-Z]+)\] Pipe to interpreter(?:: (?P<detail>[^;]+))?"
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


def _localize_tirith_finding(finding: Mapping[str, object]) -> str:
    severity = str(finding.get("severity") or "").upper()
    rule_id = str(finding.get("rule_id") or "")
    title = str(finding.get("title") or "").strip()
    description = str(finding.get("description") or "").strip()
    localized_title = _TIRITH_RULE_TEXT.get(rule_id) or _TIRITH_TITLE_TEXT.get(title) or title

    localized_description = description
    pipe_match = _PIPE_TO_INTERPRETER_RE.match(description)
    if pipe_match:
        producer, interpreter = pipe_match.groups()
        localized_description = (
            f"命令将“{producer}”的输出直接传给解释器“{interpreter}”执行，"
            "下载内容未经检查即会运行。"
        )

    prefix = f"[{_SEVERITY_TEXT.get(severity, severity)}] " if severity else ""
    if localized_title and localized_description:
        return f"{prefix}{localized_title}：{localized_description}"
    if localized_title:
        return f"{prefix}{localized_title}"
    return localized_description


def _localize_tirith_description(description: str) -> str:
    body = description.removeprefix("Security scan — ").removeprefix("Security scan: ")
    pipe_match = _TIRITH_PIPE_DESCRIPTION_RE.match(body)
    if pipe_match:
        severity = _SEVERITY_TEXT.get(pipe_match.group("severity"), pipe_match.group("severity"))
        detail = pipe_match.group("detail") or ""
        detail_match = _PIPE_TO_INTERPRETER_RE.match(detail)
        if detail_match:
            producer, interpreter = detail_match.groups()
            detail = (
                f"命令将“{producer}”的输出直接传给解释器“{interpreter}”执行，"
                "下载内容未经检查即会运行。"
            )
        suffix = body[pipe_match.end():].lstrip("; ")
        localized = f"安全扫描：[{severity}] 管道传入解释器" + (f"：{detail}" if detail else "")
        return localized + (f"；{_localize_reason_list(suffix)}" if suffix else "")
    if not body:
        return "安全扫描：检测到安全风险"
    if body == "security issue detected":
        return "安全扫描：检测到安全风险"
    for source, translated in _SEVERITY_TEXT.items():
        body = body.replace(f"[{source}]", f"[{translated}]")
    if body == "security warning detected (details unavailable)":
        return "安全扫描：检测到安全警告（详情不可用）"
    if body.startswith("tirith timed out"):
        return "安全扫描：Tirith 扫描超时"
    if body.startswith("tirith unavailable"):
        return "安全扫描：Tirith 不可用"
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
    if description.startswith("Security scan"):
        findings = payload.get("tirith_findings")
        if isinstance(findings, list):
            parts = [
                _localize_tirith_finding(item)
                for item in findings
                if isinstance(item, Mapping)
            ]
            if parts:
                payload["display_description_zh"] = "安全扫描：" + "；".join(parts)
                return payload
        payload["display_description_zh"] = _localize_tirith_description(description)
    else:
        payload["display_description_zh"] = _localize_reason_list(description)
    return payload
