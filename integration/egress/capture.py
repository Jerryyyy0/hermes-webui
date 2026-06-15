from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

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
    if not port.isdigit():
        return token, None
    # A bare IPv4 address (no port) must not be split, e.g. ICMP "8.8.8.8".
    try:
        ipaddress.ip_address(token)
        return token, None
    except ValueError:
        pass
    return host, int(port)


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


def list_capture_files(capture_dir: str, prefix: str) -> list[Path]:
    """Return capture files matching prefix, newest (by mtime) first. Empty if dir missing.

    Files that vanish between globbing and stat (e.g. concurrent tcpdump rotation)
    are skipped rather than raising.
    """
    base = Path(capture_dir)
    if not base.is_dir():
        return []
    stamped: list[tuple[float, Path]] = []
    for p in base.glob(prefix + "*"):
        try:
            if not p.is_file():
                continue
            stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in stamped]


def parse_pcap_file(path: Path, verdict: str) -> list[dict]:
    """Run `tcpdump -r path` and parse its lines into records.

    Raises FileNotFoundError if tcpdump is not installed. Tolerates non-zero
    return codes (e.g. truncated dump file) by parsing whatever was produced.
    """
    proc = subprocess.run(
        ["tcpdump", "-nn", "-tttt", "-r", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    records: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        rec = parse_tcpdump_line(line, verdict)
        if rec is not None:
            records.append(rec)
    return records
