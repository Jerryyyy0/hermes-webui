from unittest.mock import patch

from integration.egress import capture
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


def test_parse_icmp_portless_does_not_corrupt_ip():
    line = (
        "2026-06-15 11:20:32.000000 IP 172.17.0.5 > 8.8.8.8: "
        "ICMP echo request, id 1, seq 1, length 64"
    )
    rec = parse_tcpdump_line(line, "allowed")
    assert rec["proto"] == "icmp"
    assert rec["src"] == "172.17.0.5"
    assert rec["sport"] is None
    assert rec["dst"] == "8.8.8.8"
    assert rec["dport"] is None
    assert rec["length"] == 64


def test_list_capture_files_sorted_by_mtime(tmp_path):
    a = tmp_path / "allowed.pcap"
    b = tmp_path / "allowed.pcap1"
    a.write_text("x")
    b.write_text("y")
    import os
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    files = capture.list_capture_files(str(tmp_path), "allowed.pcap")
    # 新的在前
    assert [p.name for p in files] == ["allowed.pcap1", "allowed.pcap"]


def test_list_capture_files_missing_dir_returns_empty():
    assert capture.list_capture_files("/no/such/dir", "allowed.pcap") == []


def test_parse_pcap_file_runs_tcpdump_and_parses(tmp_path):
    f = tmp_path / "allowed.pcap"
    f.write_text("binary")
    fake_stdout = (
        "2026-06-15 11:20:30.123456 IP 172.17.0.5.43210 > 8.8.8.8.443: Flags [S], length 0\n"
        "garbage line\n"
    )
    with patch("integration.egress.capture.subprocess.run") as run:
        run.return_value = type("P", (), {"stdout": fake_stdout, "stderr": "", "returncode": 0})()
        recs = capture.parse_pcap_file(f, "allowed")
    assert len(recs) == 1
    assert recs[0]["dst"] == "8.8.8.8"
    assert recs[0]["verdict"] == "allowed"
