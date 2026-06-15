# Egress 流量记录与读取接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在容器内用 NFLOG + tcpdump 记录出口流量（放行/拒绝都记录），并提供 HTTP 接口从容器外取最新 N 条或全量日志，每条标注 verdict。

**Architecture:** `rules.py` 生成的 iptables 规则在 OUTPUT 判定处插入 NFLOG（放行→group 100，拒绝→group 200）；容器启动命令常驻两个 tcpdump，分别把两个 group 写成轮转 pcap；新模块 `capture.py` 用 `tcpdump -r` 文本解析 pcap，`handlers.py` 增加 `GET /api/integration/egress/traffic` 路由读取并合并两路、按时间倒序返回。

**Tech Stack:** Python 3.11 标准库（subprocess/urllib/pathlib/ipaddress）、iptables NFLOG、tcpdump；测试用 pytest。无新增第三方依赖。

参考 spec：`integration/egress/2026-06-15-egress-traffic-capture-design.md`

---

## File Structure

- `integration/egress/rules.py`（改）：whitelist/open 规则注入 NFLOG + `EGRESS_ALLOW` 链；新增 group 参数。
- `integration/config.py`（改）：新增 capture 目录与 NFLOG group 的 env 读取函数。
- `integration/egress/capture.py`（新）：定位 pcap 文件、`tcpdump -r` 文本解析为记录、合并/排序/截取最新 N。
- `integration/egress/handlers.py`（改）：`try_handle_get` 增加 `/traffic` 路由；调用 config 取 group 传给 rules 生成。
- `integration/tests/egress/test_rules.py`（改）：更新 whitelist 断言并新增 NFLOG 断言。
- `integration/tests/egress/test_capture.py`（新）：解析与合并逻辑单测。
- `integration/tests/egress/test_handlers.py`（改）：`/traffic` 路由单测。
- `integration/egress/2026-06-15-egress-traffic-capture-design.md` 末尾追加「容器启动命令」运维说明（含 `apt-get install tcpdump` 与两条 tcpdump 命令）。

`api/routes.py` 无需修改：`/traffic` 在现有 `try_handle_get`（routes.py:6432 已接入）内部处理。

---

## Task 1: config.py 新增 capture 配置读取

**Files:**
- Modify: `integration/config.py`
- Test: `integration/tests/egress/test_config_capture.py`（新）

- [ ] **Step 1: Write the failing test**

Create `integration/tests/egress/test_config_capture.py`:

```python
import importlib

import integration.config as config


def test_capture_dir_default(monkeypatch):
    monkeypatch.delenv("HERMES_EGRESS_CAPTURE_DIR", raising=False)
    assert config.egress_capture_dir() == "/var/log/egress"


def test_capture_dir_override(monkeypatch):
    monkeypatch.setenv("HERMES_EGRESS_CAPTURE_DIR", "/tmp/cap")
    assert config.egress_capture_dir() == "/tmp/cap"


def test_nflog_groups_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", raising=False)
    monkeypatch.delenv("HERMES_EGRESS_NFLOG_GROUP_DENIED", raising=False)
    assert config.egress_nflog_group_allowed() == 100
    assert config.egress_nflog_group_denied() == 200


def test_nflog_group_override_and_bad_value(monkeypatch):
    monkeypatch.setenv("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", "111")
    monkeypatch.setenv("HERMES_EGRESS_NFLOG_GROUP_DENIED", "not-a-number")
    assert config.egress_nflog_group_allowed() == 111
    # 非法值回落默认
    assert config.egress_nflog_group_denied() == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_config_capture.py -v`
Expected: FAIL with `AttributeError: module 'integration.config' has no attribute 'egress_capture_dir'`

- [ ] **Step 3: Write minimal implementation**

Append to `integration/config.py`:

```python
def egress_capture_dir() -> str:
    return str(os.getenv("HERMES_EGRESS_CAPTURE_DIR", "").strip() or "/var/log/egress")


def _egress_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def egress_nflog_group_allowed() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", 100)


def egress_nflog_group_denied() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_DENIED", 200)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_config_capture.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add integration/config.py integration/tests/egress/test_config_capture.py
git commit -m "feat(egress): add capture dir and NFLOG group config"
```

---

## Task 2: rules.py 注入 NFLOG（whitelist）

**Files:**
- Modify: `integration/egress/rules.py`
- Test: `integration/tests/egress/test_rules.py`

- [ ] **Step 1: Update existing test + add NFLOG assertions**

In `integration/tests/egress/test_rules.py`, replace `test_whitelist_rules_default_drop_and_allow_loopback_and_established` with:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_rules.py -v`
Expected: FAIL（旧实现没有 `EGRESS_ALLOW`/NFLOG，且不接受 `nflog_group_allowed` 参数）

- [ ] **Step 3: Rewrite `generate_iptables_whitelist`**

In `integration/egress/rules.py`, replace `generate_iptables_whitelist` with:

```python
def generate_iptables_whitelist(
    allowed_ips: list[str],
    *,
    nflog_group_allowed: int = 100,
    nflog_group_denied: int = 200,
) -> str:
    """Generate iptables-restore rules content (default drop + allowlist + NFLOG audit)."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_rules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add integration/egress/rules.py integration/tests/egress/test_rules.py
git commit -m "feat(egress): NFLOG audit + EGRESS_ALLOW chain in whitelist rules"
```

---

## Task 3: rules.py 注入 NFLOG（open）

**Files:**
- Modify: `integration/egress/rules.py`
- Test: `integration/tests/egress/test_rules.py`

- [ ] **Step 1: Add failing test**

Append to `integration/tests/egress/test_rules.py`:

```python
def test_open_rules_nflog_new_connections():
    text = generate_iptables_open_rules()
    assert ":OUTPUT ACCEPT" in text
    assert "--ctstate NEW -j NFLOG --nflog-group 100" in text


def test_open_rules_custom_group():
    text = generate_iptables_open_rules(nflog_group_allowed=33)
    assert "--ctstate NEW -j NFLOG --nflog-group 33" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_rules.py -k open -v`
Expected: FAIL（open 规则无 NFLOG，且不接受参数）

- [ ] **Step 3: Rewrite `generate_iptables_open_rules`**

In `integration/egress/rules.py`, replace `generate_iptables_open_rules` with:

```python
def generate_iptables_open_rules(*, nflog_group_allowed: int = 100) -> str:
    """Generate iptables-restore rules content (accept all + NFLOG new egress for audit)."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_rules.py -v`
Expected: PASS（含旧的 `test_open_rules_contains_accept_defaults`）

- [ ] **Step 5: Commit**

```bash
git add integration/egress/rules.py integration/tests/egress/test_rules.py
git commit -m "feat(egress): NFLOG audit for new egress in open policy"
```

---

## Task 4: capture.py 解析单行

**Files:**
- Create: `integration/egress/capture.py`
- Test: `integration/tests/egress/test_capture.py`（新）

- [ ] **Step 1: Write the failing test**

Create `integration/tests/egress/test_capture.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'integration.egress.capture'`

- [ ] **Step 3: Create `integration/egress/capture.py` with the parser**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_capture.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add integration/egress/capture.py integration/tests/egress/test_capture.py
git commit -m "feat(egress): tcpdump line parser for traffic records"
```

---

## Task 5: capture.py 文件定位 + 解析单个 pcap

**Files:**
- Modify: `integration/egress/capture.py`
- Test: `integration/tests/egress/test_capture.py`

- [ ] **Step 1: Add failing tests**

Append to `integration/tests/egress/test_capture.py`:

```python
from unittest.mock import patch

from integration.egress import capture


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_capture.py -k "list_capture or parse_pcap" -v`
Expected: FAIL with `AttributeError: module 'integration.egress.capture' has no attribute 'list_capture_files'`

- [ ] **Step 3: Add functions to `integration/egress/capture.py`**

Add imports at top (below `import re`):

```python
import subprocess
from pathlib import Path
```

Append functions:

```python
def list_capture_files(capture_dir: str, prefix: str) -> list[Path]:
    """Return capture files matching prefix, newest (by mtime) first. Empty if dir missing."""
    base = Path(capture_dir)
    if not base.is_dir():
        return []
    files = [p for p in base.glob(prefix + "*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_capture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add integration/egress/capture.py integration/tests/egress/test_capture.py
git commit -m "feat(egress): locate and parse rotated pcap files"
```

---

## Task 6: capture.py read_traffic 合并/排序/截取

**Files:**
- Modify: `integration/egress/capture.py`
- Test: `integration/tests/egress/test_capture.py`

- [ ] **Step 1: Add failing tests**

Append to `integration/tests/egress/test_capture.py`:

```python
def _rec(ts, verdict, dst):
    return {"ts": ts, "verdict": verdict, "proto": "tcp", "src": "172.17.0.5",
            "sport": 1, "dst": dst, "dport": 443, "length": 0, "flags": "S"}


def test_read_traffic_merges_and_sorts_desc_and_limits():
    allowed = [_rec("2026-06-15 11:00:00.000000", "allowed", "1.1.1.1"),
               _rec("2026-06-15 11:00:02.000000", "allowed", "1.1.1.2")]
    denied = [_rec("2026-06-15 11:00:01.000000", "denied", "9.9.9.9")]

    def fake_collect(capture_dir, prefix, verdict):
        return allowed if verdict == "allowed" else denied

    with patch("integration.egress.capture._collect_records", side_effect=fake_collect):
        recs = capture.read_traffic("/cap", limit=2, all_records=False, verdict=None)
    # 时间倒序
    assert [r["ts"] for r in recs] == [
        "2026-06-15 11:00:02.000000",
        "2026-06-15 11:00:01.000000",
    ]


def test_read_traffic_verdict_filter():
    allowed = [_rec("2026-06-15 11:00:00.000000", "allowed", "1.1.1.1")]
    denied = [_rec("2026-06-15 11:00:01.000000", "denied", "9.9.9.9")]

    def fake_collect(capture_dir, prefix, verdict):
        return allowed if verdict == "allowed" else denied

    with patch("integration.egress.capture._collect_records", side_effect=fake_collect):
        recs = capture.read_traffic("/cap", limit=100, all_records=False, verdict="denied")
    assert len(recs) == 1
    assert recs[0]["verdict"] == "denied"


def test_read_traffic_all_ignores_limit():
    allowed = [_rec(f"2026-06-15 11:00:0{i}.000000", "allowed", "1.1.1.1") for i in range(5)]

    def fake_collect(capture_dir, prefix, verdict):
        return allowed if verdict == "allowed" else []

    with patch("integration.egress.capture._collect_records", side_effect=fake_collect):
        recs = capture.read_traffic("/cap", limit=2, all_records=True, verdict=None)
    assert len(recs) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_capture.py -k read_traffic -v`
Expected: FAIL with `AttributeError: ... has no attribute 'read_traffic'`

- [ ] **Step 3: Add `_collect_records` and `read_traffic`**

Append to `integration/egress/capture.py`:

```python
ALLOWED_PREFIX = "allowed.pcap"
DENIED_PREFIX = "denied.pcap"


def _collect_records(capture_dir: str, prefix: str, verdict: str) -> list[dict]:
    records: list[dict] = []
    for path in list_capture_files(capture_dir, prefix):
        records.extend(parse_pcap_file(path, verdict))
    return records


def read_traffic(
    capture_dir: str,
    *,
    limit: int,
    all_records: bool,
    verdict: str | None,
) -> list[dict]:
    """Read, merge, sort (newest first) and slice egress traffic records.

    verdict: None for both, or 'allowed'/'denied' to filter.
    Raises FileNotFoundError if tcpdump is missing (propagated from parse).
    """
    records: list[dict] = []
    if verdict in (None, "allowed"):
        records.extend(_collect_records(capture_dir, ALLOWED_PREFIX, "allowed"))
    if verdict in (None, "denied"):
        records.extend(_collect_records(capture_dir, DENIED_PREFIX, "denied"))
    # ts 为定宽字符串，字典序即时间序
    records.sort(key=lambda r: r["ts"], reverse=True)
    if all_records:
        return records
    return records[: max(0, limit)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_capture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add integration/egress/capture.py integration/tests/egress/test_capture.py
git commit -m "feat(egress): merge/sort/limit traffic records read API"
```

---

## Task 7: handlers.py 增加 /traffic 路由

**Files:**
- Modify: `integration/egress/handlers.py`
- Test: `integration/tests/egress/test_handlers.py`

- [ ] **Step 1: Add failing tests**

Append to `integration/tests/egress/test_handlers.py`:

```python
def test_get_traffic_returns_records():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?limit=2")
    fake = [
        {"ts": "2026-06-15 11:00:02.000000", "verdict": "allowed", "proto": "tcp",
         "src": "172.17.0.5", "sport": 1, "dst": "1.1.1.1", "dport": 443, "length": 0, "flags": "S"},
        {"ts": "2026-06-15 11:00:01.000000", "verdict": "denied", "proto": "tcp",
         "src": "172.17.0.5", "sport": 2, "dst": "9.9.9.9", "dport": 443, "length": 0, "flags": "S"},
    ]
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", return_value=fake) as rt:
                assert try_handle_get(handler, parsed) is True
                assert rt.call_args.kwargs["limit"] == 2
                assert rt.call_args.kwargs["all_records"] is False
                assert rt.call_args.kwargs["verdict"] is None
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["records"][0]["verdict"] == "allowed"


def test_get_traffic_all_and_verdict_params():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?all=1&verdict=denied")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", return_value=[]) as rt:
                assert try_handle_get(handler, parsed) is True
                assert rt.call_args.kwargs["all_records"] is True
                assert rt.call_args.kwargs["verdict"] == "denied"
    payload = _json_payload(handler)
    assert payload["count"] == 0


def test_get_traffic_bad_verdict_rejected():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic?verdict=bogus")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_get_traffic_tcpdump_missing_returns_500():
    handler = MagicMock()
    parsed = urlparse("/api/integration/egress/traffic")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_capture_dir", return_value="/cap"):
            with patch("integration.egress.handlers.read_traffic", side_effect=FileNotFoundError("tcpdump")):
                assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(500)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_handlers.py -k traffic -v`
Expected: FAIL（`/traffic` 当前不被处理，`try_handle_get` 返回 False；patch 的 `egress_capture_dir`/`read_traffic` 名称在 handlers 中尚不存在 → AttributeError）

- [ ] **Step 3: Update `integration/egress/handlers.py`**

Update imports near the top:

```python
from urllib.parse import parse_qs

from integration.config import egress_policy_enabled, egress_policy_rules_path
from integration.config import egress_capture_dir, egress_nflog_group_allowed, egress_nflog_group_denied
from integration.egress.apply import apply_iptables_rules, get_current_iptables_rules
from integration.egress.capture import read_traffic
from integration.egress.rules import generate_iptables_open_rules, generate_iptables_whitelist, normalize_allowed_ips
```

Replace the body of `try_handle_get` with:

```python
def try_handle_get(handler, parsed) -> bool:
    if not egress_policy_enabled():
        return False
    if parsed.path == "/api/integration/egress/policy":
        result = get_current_iptables_rules()
        if not result.ok:
            bad(handler, result.error or "failed", status=500)
            return True
        j(handler, {"ok": True, "rules": result.stdout})
        return True
    if parsed.path == "/api/integration/egress/traffic":
        return _handle_traffic_get(handler, parsed)
    return False


def _handle_traffic_get(handler, parsed) -> bool:
    qs = parse_qs(parsed.query)
    verdict = (qs.get("verdict", [None])[0] or None)
    if verdict is not None:
        verdict = verdict.strip().lower()
        if verdict not in ("allowed", "denied"):
            bad(handler, "verdict must be 'allowed' or 'denied'", status=400)
            return True
    all_records = (qs.get("all", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
    raw_limit = (qs.get("limit", ["100"])[0]).strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = 100
    if limit < 0:
        limit = 0
    try:
        records = read_traffic(
            egress_capture_dir(),
            limit=limit,
            all_records=all_records,
            verdict=verdict,
        )
    except FileNotFoundError:
        bad(handler, "tcpdump not found in container", status=500)
        return True
    except Exception as exc:
        bad(handler, _sanitize_error(exc), status=500)
        return True
    j(handler, {"ok": True, "count": len(records), "records": records})
    return True
```

Note: `egress_nflog_group_allowed`/`egress_nflog_group_denied` are imported now and consumed in Task 8.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_handlers.py -v`
Expected: PASS（含原有 policy 测试）

- [ ] **Step 5: Commit**

```bash
git add integration/egress/handlers.py integration/tests/egress/test_handlers.py
git commit -m "feat(egress): GET /traffic route to read captured egress logs"
```

---

## Task 8: handlers.py 把 NFLOG group 传给规则生成

**Files:**
- Modify: `integration/egress/handlers.py`
- Test: `integration/tests/egress/test_handlers.py`

- [ ] **Step 1: Add failing test**

Append to `integration/tests/egress/test_handlers.py`:

```python
def test_post_whitelist_passes_nflog_groups_from_config():
    handler = MagicMock()
    handler.client_address = ("1.2.3.4", 7777)
    parsed = urlparse("/api/integration/egress/policy")
    with patch("integration.egress.handlers.egress_policy_enabled", return_value=True):
        with patch("integration.egress.handlers.egress_policy_rules_path", return_value="/tmp/rules.v4"):
            with patch("integration.egress.handlers.egress_nflog_group_allowed", return_value=111):
                with patch("integration.egress.handlers.egress_nflog_group_denied", return_value=222):
                    with patch("integration.egress.handlers.generate_iptables_whitelist", return_value="RULES") as gen:
                        with patch("integration.egress.handlers.apply_iptables_rules",
                                   return_value=ApplyResult(ok=True, rules_path="/tmp/rules.v4")):
                            body = {"policy_type": "whitelist", "allowed_ips": ["10.0.0.0/24"]}
                            assert try_handle_post(handler, parsed, body) is True
    assert gen.call_args.kwargs["nflog_group_allowed"] == 111
    assert gen.call_args.kwargs["nflog_group_denied"] == 222
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest integration/tests/egress/test_handlers.py -k nflog_groups -v`
Expected: FAIL（当前调用 `generate_iptables_whitelist(normalized)` 不带 group 参数）

- [ ] **Step 3: Update the two generate calls in `try_handle_post`**

In `integration/egress/handlers.py`, change the `open` branch:

```python
        if policy_type == "open":
            rules = generate_iptables_open_rules(nflog_group_allowed=egress_nflog_group_allowed())
            payload: dict = {"policy_type": "open"}
```

And the `whitelist` branch's rule generation line:

```python
            rules = generate_iptables_whitelist(
                normalized,
                nflog_group_allowed=egress_nflog_group_allowed(),
                nflog_group_denied=egress_nflog_group_denied(),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest integration/tests/egress/test_handlers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add integration/egress/handlers.py integration/tests/egress/test_handlers.py
git commit -m "feat(egress): wire configurable NFLOG groups into policy apply"
```

---

## Task 9: 运维文档（容器启动命令）

**Files:**
- Modify: `integration/egress/2026-06-15-egress-traffic-capture-design.md`

- [ ] **Step 1: Append an "运维：容器启动命令" section**

Append to the design doc:

````markdown
## 运维：容器启动命令

在容器启动命令的 `apt-get install` 行加入 `tcpdump`，并在 `server.py` 前常驻两个 tcpdump：

```bash
-c "apt-get update && apt-get install -y --no-install-recommends iptables tcpdump \
  && mkdir -p /var/log/egress \
  && ( tcpdump -i nflog:100 -nn -U -w /var/log/egress/allowed.pcap -C 10 -W 5 -Z root & ) \
  && ( tcpdump -i nflog:200 -nn -U -w /var/log/egress/denied.pcap  -C 10 -W 5 -Z root & ) \
  && cd /home/hermeswebui/.hermes/hermes-webui \
  && ( /app/venv/bin/hermes gateway run & ) && sleep 3 && /app/venv/bin/python server.py"
```

说明：
- group/目录/轮转由 env 调整：`HERMES_EGRESS_CAPTURE_DIR`、`HERMES_EGRESS_NFLOG_GROUP_ALLOWED`、`HERMES_EGRESS_NFLOG_GROUP_DENIED`。改了 group 时上面 `nflog:100/200` 也要同步。
- 只有应用过策略（`POST /policy`，open 或 whitelist）后才会产生 NFLOG 记录。若希望容器一起来就抓，可在 `sleep 3` 后追加：
  `&& curl -s -XPOST http://localhost:8787/api/integration/egress/policy -H 'Content-Type: application/json' -d '{"policy_type":"open"}'`
- 读取：`curl -s 'http://localhost:18787/api/integration/egress/traffic?limit=50' | jq`
````

- [ ] **Step 2: Commit**

```bash
git add integration/egress/2026-06-15-egress-traffic-capture-design.md
git commit -m "docs(egress): container startup command for traffic capture"
```

---

## Task 10: 全量回归

**Files:** 无（仅运行）

- [ ] **Step 1: Run the whole egress test suite**

Run: `python -m pytest integration/tests/egress/ -v`
Expected: 全部 PASS

- [ ] **Step 2: Run the integration seam test (确保禁用时仍 noop)**

Run: `python -m pytest tests/test_integration_seam.py -v`
Expected: 全部 PASS

- [ ] **Step 3: Lint 改动文件**

Run: `python -m ruff check integration/egress/ integration/config.py`
Expected: no errors（若仓库未装 ruff 则跳过）

---

## Self-Review 结论

- **Spec 覆盖**：组件一(NFLOG 规则)→Task 2/3；组件二(采集/启动/config)→Task 1/9；组件三(capture 解析+/traffic 路由)→Task 4/5/6/7；verdict 标注→Task 6(按来源 pcap)；组件四(测试)→各 Task 内含 + Task 10。无遗漏。
- **占位符**：无 TBD/TODO，所有步骤含完整代码与命令。
- **类型/命名一致**：`read_traffic(capture_dir, *, limit, all_records, verdict)`、`list_capture_files(capture_dir, prefix)`、`parse_pcap_file(path, verdict)`、`parse_tcpdump_line(line, verdict)`、`_collect_records(capture_dir, prefix, verdict)`、`ALLOWED_PREFIX/DENIED_PREFIX` 在 Task 4-7 间一致；`generate_iptables_whitelist(allowed_ips, *, nflog_group_allowed, nflog_group_denied)` 与 `generate_iptables_open_rules(*, nflog_group_allowed)` 在 Task 2/3/8 间一致。
