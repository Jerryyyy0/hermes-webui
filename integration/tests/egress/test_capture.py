from integration.egress.capture import parse_tcpdump_line


def test_parse_tcp_syn_line():
    line = (
        "2026-06-15 11:20:30.123456 IP 172.17.0.5.43210 > 8.8.8.8.443: "
        "Flags [S], seq 0, win 64240, length 0"
    )
    rec = parse_tcpdump_line(line, "denied")
    assert rec == {
        "ts": "2026-06-15 11:20:30.123456",
        "verdict": "denied",
        "proto": "tcp",
        "src": "172.17.0.5",
        "sport": 43210,
        "dst": "8.8.8.8",
        "dport": 443,
        "length": 0,
        "flags": "S",
    }


def test_parse_udp_line():
    line = (
        "2026-06-15 11:20:31.000000 IP 172.17.0.5.51000 > 8.8.8.8.53: "
        "UDP, length 29"
    )
    rec = parse_tcpdump_line(line, "allowed")
    assert rec["proto"] == "udp"
    assert rec["dport"] == 53
    assert rec["length"] == 29
    assert rec["flags"] == ""


def test_parse_garbage_line_returns_none():
    assert parse_tcpdump_line("reading from file x.pcap, link-type ...", "allowed") is None
    assert parse_tcpdump_line("", "allowed") is None
