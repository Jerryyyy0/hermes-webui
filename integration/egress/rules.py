from __future__ import annotations

import ipaddress
from collections.abc import Iterable


def _normalize_allowed_ip(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("allowed_ips contains empty value")
    try:
        if "/" in raw:
            return str(ipaddress.ip_network(raw, strict=False))
        return str(ipaddress.ip_address(raw))
    except ValueError as exc:
        raise ValueError(f"invalid ip/cidr: {raw}") from exc


def normalize_allowed_ips(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        ip = _normalize_allowed_ip(value)
        if ip in seen:
            continue
        seen.add(ip)
        normalized.append(ip)
    if not normalized:
        raise ValueError("allowed_ips required")
    return normalized


def generate_iptables_open_rules(*, nflog_group_allowed: int = 100) -> str:
    """生成 iptables-restore 规则内容（全部放行 + 对新出口连接做 NFLOG 审计）。"""
    rules: list[str] = []
    rules.append("*filter")
    rules.append("")
    rules.append("# 默认策略：允许所有流量")
    rules.append(":INPUT ACCEPT [0:0]")
    rules.append(":FORWARD ACCEPT [0:0]")
    rules.append(":OUTPUT ACCEPT [0:0]")
    rules.append("")
    rules.append("# 允许本地回环接口")
    rules.append("-A OUTPUT -o lo -j ACCEPT")
    rules.append("")
    rules.append("# 已建立/相关连接不记录以降噪")
    rules.append("-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("")
    rules.append("# 审计：仅对新出口连接记 NFLOG（放行）")
    rules.append(f"-A OUTPUT -m conntrack --ctstate NEW -j NFLOG --nflog-group {nflog_group_allowed}")
    rules.append("")
    rules.append("COMMIT")
    return "\n".join(rules)


def generate_iptables_whitelist(
    allowed_ips: list[str],
    *,
    nflog_group_allowed: int = 100,
    nflog_group_denied: int = 200,
) -> str:
    """生成 iptables-restore 规则内容（默认拒绝 + 白名单放行 + NFLOG 审计）。"""
    ips = normalize_allowed_ips(allowed_ips)
    rules: list[str] = []
    rules.append("*filter")
    rules.append("")
    rules.append("# 默认策略：拒绝所有流量")
    rules.append(":INPUT DROP [0:0]")
    rules.append(":FORWARD DROP [0:0]")
    rules.append(":OUTPUT DROP [0:0]")
    rules.append(":EGRESS_ALLOW - [0:0]")
    rules.append("")
    rules.append("# 允许本地回环接口")
    rules.append("-A INPUT -i lo -j ACCEPT")
    rules.append("-A OUTPUT -o lo -j ACCEPT")
    rules.append("")
    rules.append("# 允许已建立和相关连接的响应流量（回包/续传，不记录以降噪）")
    rules.append("-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("")
    rules.append("# 放行路径：先记 NFLOG（放行）再 ACCEPT")
    rules.append(f"-A EGRESS_ALLOW -j NFLOG --nflog-group {nflog_group_allowed}")
    rules.append("-A EGRESS_ALLOW -j ACCEPT")
    rules.append("")
    rules.append("# 白名单：允许指定的 IP 地址")
    for ip in ips:
        rules.append(f"# 允许 IP: {ip}")
        rules.append(f"-A INPUT -s {ip} -j ACCEPT")
        rules.append(f"-A OUTPUT -d {ip} -j EGRESS_ALLOW")
        rules.append("")
    rules.append("# 兜底：被拒绝的出口包先记 NFLOG（拒绝）再 DROP")
    rules.append(f"-A OUTPUT -j NFLOG --nflog-group {nflog_group_denied}")
    rules.append("-A OUTPUT -j DROP")
    rules.append("")
    rules.append("COMMIT")
    return "\n".join(rules)

