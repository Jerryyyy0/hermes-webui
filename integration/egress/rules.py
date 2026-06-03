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


def generate_iptables_open_rules() -> str:
    """Generate iptables-restore rules content (accept all)."""
    rules: list[str] = []
    rules.append("*filter")
    rules.append("")
    rules.append("# 默认策略：允许所有流量")
    rules.append(":INPUT ACCEPT [0:0]")
    rules.append(":FORWARD ACCEPT [0:0]")
    rules.append(":OUTPUT ACCEPT [0:0]")
    rules.append("")
    rules.append("COMMIT")
    return "\n".join(rules)


def generate_iptables_whitelist(allowed_ips: list[str]) -> str:
    """Generate iptables-restore rules content (default drop + allowlist)."""
    ips = normalize_allowed_ips(allowed_ips)
    rules: list[str] = []
    rules.append("*filter")
    rules.append("")
    rules.append("# 默认策略：拒绝所有流量")
    rules.append(":INPUT DROP [0:0]")
    rules.append(":FORWARD DROP [0:0]")
    rules.append(":OUTPUT DROP [0:0]")
    rules.append("")
    rules.append("# 允许本地回环接口")
    rules.append("-A INPUT -i lo -j ACCEPT")
    rules.append("-A OUTPUT -o lo -j ACCEPT")
    rules.append("")
    rules.append("# 允许已建立和相关连接的响应流量")
    rules.append("-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    rules.append("")
    rules.append("# 白名单：允许指定的 IP 地址")
    for ip in ips:
        rules.append(f"# 允许 IP: {ip}")
        rules.append(f"-A INPUT -s {ip} -j ACCEPT")
        rules.append(f"-A OUTPUT -d {ip} -j ACCEPT")
        rules.append("")
    rules.append("# 可选：允许 DNS 查询（如需要）")
    rules.append("#-A OUTPUT -p udp --dport 53 -j ACCEPT")
    rules.append("#-A OUTPUT -p tcp --dport 53 -j ACCEPT")
    rules.append("")
    rules.append("COMMIT")
    return "\n".join(rules)

