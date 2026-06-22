"""出口流量抓包记录的读取与解析。

容器侧由常驻的 tcpdump 把 NFLOG 通道（放行 group / 拒绝 group）写成轮转 pcap，
本模块负责定位这些 pcap、用 ``tcpdump -r`` 文本解析成结构化记录，并对外提供
``read_traffic`` 供接口取最新 N 条或全量日志。不引入 scapy 等额外依赖。
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

# 解析 `tcpdump -nn -tttt` 单行输出的正则：时间戳、源、目的、其余部分。
_TS = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
_LINE_RE = re.compile(
    rf"^{_TS} IP6? (?P<src>\S+) > (?P<dst>[^:]+): (?P<rest>.*)$"
)
_FLAGS_RE = re.compile(r"Flags \[(?P<flags>[^\]]*)\]")
_LENGTH_RE = re.compile(r"length (?P<length>\d+)")


def _split_host_port(token: str) -> tuple[str, int | None]:
    """把 ``host.port`` 形式的 token 拆成 (主机, 端口)；无端口时端口返回 None。"""
    if "." not in token:
        return token, None
    host, _, port = token.rpartition(".")
    if not port.isdigit():
        return token, None
    # 不带端口的裸 IPv4 地址不能被拆分，例如 ICMP 的 "8.8.8.8"（否则会被误截成 "8.8.8" + 8）。
    try:
        ipaddress.ip_address(token)
        return token, None
    except ValueError:
        pass
    return host, int(port)


def parse_tcpdump_line(line: str, verdict: str) -> dict | None:
    """把一行 ``tcpdump -nn -tttt`` 文本解析成记录；无法解析则返回 None。"""
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    src_host, sport = _split_host_port(m.group("src"))
    dst_host, dport = _split_host_port(m.group("dst"))
    rest = m.group("rest")

    # 根据其余部分判定协议：有 Flags[...] 即 TCP（并取出标志），否则看 UDP/ICMP 关键字。
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
    """返回匹配 prefix 的抓包文件，按修改时间从新到旧排序；目录不存在则返回空列表。

    在 glob 与 stat 之间消失的文件（如 tcpdump 轮转并发覆盖）会被跳过而不抛异常。
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
            # 文件在轮转中被删除/重建，跳过即可。
            continue
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in stamped]


def parse_pcap_file(path: Path, verdict: str) -> list[dict]:
    """对单个 pcap 跑 ``tcpdump -r`` 并把输出逐行解析成记录列表。

    tcpdump 未安装时抛出 FileNotFoundError。对非零返回码（如正在写入被截断的 dump）
    保持容忍：只解析已经产出的内容，不因返回码失败而中断。
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


# tcpdump 轮转写入的两路 pcap 文件名前缀：放行 / 拒绝。
ALLOWED_PREFIX = "allowed.pcap"
DENIED_PREFIX = "denied.pcap"


def _collect_records(capture_dir: str, prefix: str, verdict: str) -> list[dict]:
    """汇总某一路（prefix）下所有轮转文件解析出的记录，并打上对应 verdict。"""
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
    """读取、合并、按时间倒序排序并截取出口流量记录。

    verdict 为 None 时返回放行+拒绝两路；为 'allowed'/'denied' 时只返回对应一路。
    tcpdump 缺失时会抛出 FileNotFoundError（由解析层向上传递）。
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
