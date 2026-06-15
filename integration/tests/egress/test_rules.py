import pytest

from integration.egress.rules import (
    generate_iptables_open_rules,
    generate_iptables_whitelist,
    normalize_allowed_ips,
)


def test_open_rules_contains_accept_defaults():
    text = generate_iptables_open_rules()
    assert "*filter" in text
    assert ":INPUT ACCEPT" in text
    assert ":OUTPUT ACCEPT" in text
    assert "COMMIT" in text


def test_normalize_allowed_ips_accepts_ip_and_cidr_and_dedupes():
    ips = normalize_allowed_ips([" 1.2.3.4 ", "10.0.0.0/24", "10.0.0.1/24", "1.2.3.4"])
    assert ips[0] == "1.2.3.4"
    assert ips[1] == "10.0.0.0/24"
    assert len(ips) == 2


def test_normalize_allowed_ips_rejects_empty_list():
    with pytest.raises(ValueError):
        normalize_allowed_ips([])


def test_normalize_allowed_ips_rejects_invalid_entry():
    with pytest.raises(ValueError):
        normalize_allowed_ips(["nope"])


def test_whitelist_rules_default_drop_and_allow_loopback_and_established():
    text = generate_iptables_whitelist(["1.2.3.4"])
    assert ":INPUT DROP" in text
    assert "-A INPUT -i lo -j ACCEPT" in text
    assert "ESTABLISHED,RELATED" in text
    assert "-A INPUT -s 1.2.3.4 -j ACCEPT" in text
    # 出口白名单改为先经自定义链记日志再放行
    assert "-A OUTPUT -d 1.2.3.4 -j EGRESS_ALLOW" in text


def test_whitelist_rules_nflog_allowed_and_denied():
    text = generate_iptables_whitelist(["1.2.3.4"])
    assert ":EGRESS_ALLOW -" in text
    # 放行路径记 group 100
    assert "-A EGRESS_ALLOW -j NFLOG --nflog-group 100" in text
    assert "-A EGRESS_ALLOW -j ACCEPT" in text
    # 拒绝兜底记 group 200 再 DROP
    assert "-A OUTPUT -j NFLOG --nflog-group 200" in text
    assert "-A OUTPUT -j DROP" in text


def test_whitelist_rules_custom_groups():
    text = generate_iptables_whitelist(["1.2.3.4"], nflog_group_allowed=11, nflog_group_denied=22)
    assert "-A EGRESS_ALLOW -j NFLOG --nflog-group 11" in text
    assert "-A OUTPUT -j NFLOG --nflog-group 22" in text

