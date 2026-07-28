"""Build Chinese-only display copy without changing approval protocol keys."""

from __future__ import annotations

import re
from collections.abc import Mapping

from integration.project_logging import get_logger

logger = get_logger(__name__)

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
    "Windows cmd destructive delete": "Windows 命令行删除文件",
    "Windows PowerShell destructive delete": "PowerShell 删除文件",
    "PowerShell encoded command execution": "PowerShell 执行编码命令",
    "world/other-writable permissions": "设置为所有用户可写权限",
    "recursive world/other-writable (long flag)": "递归设置为所有用户可写权限",
    "recursive chown to root": "递归将文件所有者改为 root",
    "recursive chown to root (long flag)": "递归将文件所有者改为 root（长参数）",
    "stop/restart hermes gateway (kills running agents)": "停止或重启 Hermes Gateway（会终止运行中的 Agent）",
    "hermes update (restarts gateway, kills running agents)": "更新 Hermes（会重启 Gateway 并终止运行中的 Agent）",
    "docker compose restart/stop/kill/down (container lifecycle)": "操作 Docker Compose 容器生命周期",
    "docker restart/stop/kill (container lifecycle)": "操作 Docker 容器生命周期",
    "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')": "脱离 systemd 启动 Gateway",
    "kill hermes/gateway process (self-termination)": "终止 Hermes/Gateway 进程（可能导致自身停止）",
    "stop/restart hermes launchd service (kills running agents)": "停止或重启 Hermes launchd 服务（会终止运行中的 Agent）",
    "copy/move file into system config path": "复制或移动文件到系统配置目录",
    "overwrite project env/config file": "覆盖项目环境或配置文件",
    "copy/move file into sensitive credential/SSH/shell-rc path": "覆盖敏感凭据、SSH 或 shell 配置文件",
    "in-place edit of sensitive credential/SSH/shell-rc path": "原地修改敏感凭据、SSH 或 shell 配置文件",
    "in-place edit of sensitive credential/SSH/shell-rc path (long flag)": "原地修改敏感凭据、SSH 或 shell 配置文件（长参数）",
    "in-place edit of Hermes config/env": "原地修改 Hermes 配置或环境文件",
    "in-place edit of Hermes config/env (long flag)": "原地修改 Hermes 配置或环境文件（长参数）",
    "in-place edit of system config": "原地修改系统配置",
    "in-place edit of system config (long flag)": "原地修改系统配置（长参数）",
    "chmod +x followed by immediate execution": "赋予脚本执行权限后立即运行",
    "git reset --hard (destroys uncommitted changes)": "执行 git reset --hard（会丢弃未提交修改）",
    "git force push (rewrites remote history)": "强制推送 Git（会改写远程历史）",
    "git force push short flag (rewrites remote history)": "强制推送 Git（会改写远程历史）",
    "git clean with force (deletes untracked files)": "强制清理 Git 未跟踪文件",
    "git branch force delete": "强制删除 Git 分支",
    "git branch force delete (long flags)": "强制删除 Git 分支（长参数）",
    "git branch force delete (long flags, force-first)": "强制删除 Git 分支（长参数）",
    "sudo with privilege flag (stdin/askpass/shell/list)": "使用 sudo 特权参数",
    "sudo with combined-flag privilege escalation": "使用 sudo 组合参数提升权限",
    "sudo password guessing via stdin (sudo -S)": "通过标准输入尝试猜测 sudo 密码",
    "kill processes by regex (killall -r)": "通过正则匹配终止进程",
    "force kill processes (killall -KILL)": "强制终止进程（killall -KILL）",
    "force kill processes (killall -s KILL)": "强制终止进程（killall -s KILL）",
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
    r"Command pipes output from '([^']+)' directly to interpreter '([^']+)'\."
)
_PIPE_TO_INTERPRETER_SUMMARY_RE = re.compile(
    r"(?P<producer>[^:;]+?)\s*\|\s*(?P<interpreter>[^:;：]+)"
)
_SAFER_RE = re.compile(r"(?:^|\s)Safer:\s*(?P<remediation>.+)$", re.DOTALL)
_SECURITY_SCAN_PREFIX_RE = re.compile(r"^(?:Security scan|安全扫描)\s*(?:—|:|：)\s*")
_TIRITH_FINDING_PROSE_RE = re.compile(
    r"^\[(?P<severity>[A-Z]+|严重|高|中|低|提示)\]\s+"
    r"(?P<title>[^:;：]+)(?:(?:：|:)\s*(?P<detail>.*))?$"
)
_TIRITH_FINDING_BOUNDARY_RE = re.compile(
    r"\s*(?:;|；)\s*(?=\[(?:[A-Z]+|严重|高|中|低|提示)\]\s+)"
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


def _strip_tirith_remediation(description: str) -> tuple[str, str]:
    match = _SAFER_RE.search(description)
    if not match:
        return description.strip(), ""
    return description[: match.start()].rstrip(" ;；"), match.group("remediation").strip()


def _localize_tirith_remediation(remediation: str) -> str:
    remediation = str(remediation or "").strip()
    if not remediation:
        return ""
    remediation = re.sub(r"^Safer:\s*", "", remediation, flags=re.IGNORECASE)
    return f"更安全的做法：{remediation}"


def _pipe_summary(finding: Mapping[str, object], description: str) -> tuple[str, str]:
    summary = str(finding.get("command_summary") or "").strip()
    if summary:
        match = _PIPE_TO_INTERPRETER_SUMMARY_RE.search(summary)
        if match:
            return match.group("producer").strip(), match.group("interpreter").strip()
    match = _PIPE_TO_INTERPRETER_RE.search(description)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", ""


def _localize_tirith_detail(
    rule_id: str, title: str, description: str, finding: Mapping[str, object] | None = None
) -> str:
    finding = finding or {}
    description, inline_remediation = _strip_tirith_remediation(description)
    if rule_id in {"pipe-to-interpreter", "pipe_to_interpreter"} or title == "Pipe to interpreter":
        command_prefix = re.match(r"^(?P<summary>[^：:;]+[|][^：:;]+)[：:]\s*", description)
        if command_prefix and not finding.get("command_summary"):
            finding = {**finding, "command_summary": command_prefix.group("summary")}
            description = description[command_prefix.end():].strip()
        producer, interpreter = _pipe_summary(finding, description)
        if producer and interpreter:
            detail = f"命令将“{producer}”的输出直接传给解释器“{interpreter}”执行，下载内容未经检查即会运行。"
        else:
            detail = "命令将输出直接传给解释器执行，下载内容未经检查即会运行。"
        remediation = _localize_tirith_remediation(finding.get("remediation") or inline_remediation)
        return f"{detail}；{remediation}" if remediation else detail
    if rule_id in {
        "schemeless-url-in-sink-context",
        "schemeless_url_in_sink_context",
    } or title == "Schemeless URL in sink context":
        return "无协议 URL 被传给会下载或执行内容的命令，可能造成来源识别或执行风险。"
    if description in _TIRITH_DESCRIPTION_TEXT:
        detail = _TIRITH_DESCRIPTION_TEXT[description]
    else:
        detail = description
    remediation = _localize_tirith_remediation(finding.get("remediation") or inline_remediation)
    return f"{detail}；{remediation}" if remediation else detail


def _localized_unknown_title(title: str, rule_id: str) -> str:
    if title in _TIRITH_TITLE_TEXT:
        return _TIRITH_TITLE_TEXT[title]
    return title or (f"安全规则：{rule_id.removeprefix('tirith:')}" if rule_id else "未识别的安全规则")


def _localize_tirith_finding(finding: Mapping[str, object]) -> str:
    severity = str(finding.get("severity") or "").upper()
    rule_id = str(finding.get("rule_id") or "")
    title = str(finding.get("title") or "").strip()
    description = str(finding.get("description") or "").strip()
    localized_title = _TIRITH_RULE_TEXT.get(rule_id) or _TIRITH_TITLE_TEXT.get(title)
    localized_description = _localize_tirith_detail(rule_id, title, description, finding)

    prefix = f"[{_SEVERITY_TEXT.get(severity, severity)}] " if severity else ""
    if not localized_title:
        localized_title = _localized_unknown_title(title, rule_id)
    return f"{prefix}{localized_title}" + (f"：{localized_description}" if localized_description else "")


def _localize_tirith_prose_finding(segment: str) -> str:
    match = _TIRITH_FINDING_PROSE_RE.match(segment.strip())
    if not match:
        return "安全扫描发现风险（详情：" + segment.strip() + "）"
    severity = match.group("severity")
    title = (match.group("title") or "").strip()
    detail = (match.group("detail") or "").strip()
    rule_id = "pipe-to-interpreter" if title.startswith("Pipe to interpreter") else ""
    summary = ""
    if rule_id and ":" in title:
        title, summary = title.split(":", 1)
    if rule_id and "：" in title:
        title, summary = title.split("：", 1)
    return _localize_tirith_finding(
        {
            "severity": severity,
            "title": title.strip(),
            "description": detail,
            "command_summary": summary.strip(),
            "rule_id": rule_id,
        }
    )


def _localize_tirith_description(description: str) -> str:
    # Gateway mirrors and legacy clients can pre-localize only the separator
    # and severity (``Security scan：[高] …``). Treat those variants as the
    # same canonical envelope so the Tirith finding parser still sees the
    # actual ``[severity] title: detail`` payload.
    body = _SECURITY_SCAN_PREFIX_RE.sub("", description, count=1)
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
    # Only split at a separator that introduces another severity-tagged
    # finding. This supports already-localized `；[高]` payloads without
    # splitting semicolons in a rule's detail or `Safer:` remediation.
    segments = [
        part.strip()
        for part in _TIRITH_FINDING_BOUNDARY_RE.split(body)
        if part.strip()
    ]
    if any(_TIRITH_FINDING_PROSE_RE.match(segment) for segment in segments):
        return "安全扫描：" + "；".join(
            _localize_tirith_prose_finding(segment) for segment in segments
        )

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
    security_scan = bool(_SECURITY_SCAN_PREFIX_RE.match(description))
    if security_scan:
        logger.info(
            "[approval-localization-trace] entered envelope=%s structured_findings=%d",
            "zh" if description.startswith("安全扫描") else "en",
            len(payload.get("tirith_findings") or []),
        )
    if pattern_key == "execute_code" or description == _EXECUTE_CODE_DESCRIPTION:
        payload["display_description_zh"] = _EXECUTE_CODE_DESCRIPTION_ZH
        return payload
    if security_scan:
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
        pattern_parts = [
            _localize_reason(str(key))
            for key in (payload.get("pattern_keys") or [])
            if key and not str(key).startswith("tirith:")
        ]
        if pattern_parts:
            payload["display_description_zh"] += "；" + "；".join(pattern_parts)
    else:
        payload["display_description_zh"] = _localize_reason_list(description)
    return payload
