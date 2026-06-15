from __future__ import annotations

import re

_TS = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
_LINE_RE = re.compile(
    rf"^{_TS} IP6? (?P<src>\S+) > (?P<dst>[^:]+): (?P<rest>.*)$"
)
_FLAGS_RE = re.compile(r"Flags \[(?P<flags>[^\]]*)\]")
_LENGTH_RE = re.compile(r"length (?P<length>\d+)")


def _split_host_port(token: str) -> tuple[str, int | None]:
    if "." not in token:
        return token, None
    host, _, port = token.rpartition(".")
    try:
        return host, int(port)
    except ValueError:
        return token, None


def parse_tcpdump_line(line: str, verdict: str) -> dict | None:
    """Parse one `tcpdump -nn -tttt` text line into a record, or None if unparseable."""
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    src_host, sport = _split_host_port(m.group("src"))
    dst_host, dport = _split_host_port(m.group("dst"))
    rest = m.group("rest")

    flags_m = _FLAGS_RE.search(rest)
    if flags_m:
        proto = "tcp"
        flags = flags_m.group("flags")
    elif "UDP" in rest:
        proto = "udp"
        flags = ""
    elif "ICMP" in rest:
        proto = "icmp"
        flags = ""
    else:
        proto = "other"
        flags = ""

    length_m = _LENGTH_RE.search(rest)
    length = int(length_m.group("length")) if length_m else 0

    return {
        "ts": m.group("ts"),
        "verdict": verdict,
        "proto": proto,
        "src": src_host,
        "sport": sport,
        "dst": dst_host,
        "dport": dport,
        "length": length,
        "flags": flags,
    }
