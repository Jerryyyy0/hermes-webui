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
    assert "-A OUTPUT -d 1.2.3.4 -j ACCEPT" in text

