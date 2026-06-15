# Egress 流量记录与读取接口 设计文档

- 日期：2026-06-15
- 模块：`integration/egress`
- 目标：在容器内用 tcpdump 记录出口流量（放行的和被 iptables 拒绝的都记录），并通过 HTTP 接口从容器外获取全量或最新若干条日志，每条标注放行/拒绝。

## 背景与约束

- 容器已具备 `--cap-add=NET_ADMIN --cap-add=NET_RAW`，启动命令里 `apt-get install` 了 iptables。
- 现有 egress 能力：`rules.py`（生成 iptables-restore 规则）、`apply.py`（写入并 `iptables-restore` 应用、`iptables -L -n -v` 查看）、`handlers.py`（HTTP 路由 `/api/integration/egress/policy`，经 `api/routes.py` 的 GET/POST 分发接入）。
- 重要技术前提（纠正参考文档的错误说法）：Linux 出向 packet tap（AF_PACKET TX，即 `tcpdump -i eth0` 的抓取点）位于 `dev_queue_xmit`，发生在 netfilter `filter/OUTPUT` 链**之后**。被 OUTPUT 链 DROP 的包到不了网卡 tap，因此 `tcpdump -i eth0` **抓不到被拒绝的包**。要记录被拒绝的包，必须用 iptables 的 **NFLOG**：在判定前/判定处把包复制到 nflog 通道，由 `tcpdump -i nflog:<group>` 捕获。

## 设计决策（已与用户确认）

1. tcpdump 抓包进程：由**容器启动命令常驻**，integration 代码只负责读取/解析 pcap，不管理进程生命周期。
2. 解析方式：`tcpdump -r <file>` **文本解析**，零额外 Python 依赖（不引入 scapy）。
3. 容量控制：**按大小轮转 + 固定份数**（`tcpdump -C -W`），磁盘占用有硬上限。
4. 接口形态：**最新 N 条 + 可选全量**。
5. 被拒绝的包**也要进 pcap**：用 **NFLOG 全量抓取**。
6. 每条记录**标注 verdict**（allowed/denied）：用**两个 NFLOG group + 两份 pcap**，verdict 由记录来自哪份 pcap 决定（真实判定，非事后推断）。

## 架构总览

```
容器启动命令(常驻)
  ├─ 安装 iptables + tcpdump
  ├─ tcpdump -i nflog:100 → allowed.pcap (轮转)   # 放行的出口连接
  ├─ tcpdump -i nflog:200 → denied.pcap  (轮转)   # 被拒绝的出口包
  └─ 启动 server.py

iptables 规则 (rules.py 生成, 经 POST /policy 应用)
  OUTPUT 链在 ACCEPT/DROP 判定处插 NFLOG:
    放行路径 → NFLOG group 100 → ACCEPT
    拒绝兜底 → NFLOG group 200 → DROP

读取接口 (新 capture.py + handlers)
  GET /api/integration/egress/traffic?limit=100      # 最新N条(放行+拒绝合并, 时间倒序)
  GET /api/integration/egress/traffic?all=1          # 全量
  GET /api/integration/egress/traffic?verdict=denied # 可选过滤
```

## 组件一：iptables 规则改造（`rules.py`）

### whitelist 形态

```
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
:EGRESS_ALLOW - [0:0]

# 本地回环
-A INPUT -i lo -j ACCEPT
-A OUTPUT -o lo -j ACCEPT

# 已建立/相关连接(回包与续传)，不记录以降噪
-A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# 自定义链：先记日志(group 100)再放行
-A EGRESS_ALLOW -j NFLOG --nflog-group 100
-A EGRESS_ALLOW -j ACCEPT

# 白名单目的地 → 走 EGRESS_ALLOW
-A INPUT  -s <ip> -j ACCEPT
-A OUTPUT -d <ip> -j EGRESS_ALLOW

# 兜底：被拒绝的出口包先记日志(group 200)再丢
-A OUTPUT -j NFLOG --nflog-group 200
-A OUTPUT -j DROP

COMMIT
```

- 因为 `ESTABLISHED,RELATED` 在前面已放行，能走到 EGRESS_ALLOW / 兜底的基本是**新连接首包**，NFLOG 记录的是“连接尝试”而非每个数据包，日志量可控。
- INPUT 链保持原有白名单语义（放行指定源 IP），不加 NFLOG（只审计出口）。

### open 形态

```
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]

-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# 仅对新出口连接记 group 100(无拒绝, group 200 为空)
-A OUTPUT -m conntrack --ctstate NEW -j NFLOG --nflog-group 100

COMMIT
```

### 参数化

- NFLOG group 号默认 100（allowed）/ 200（denied），通过 env 可配。
- 现有 `normalize_allowed_ips`、`generate_iptables_open_rules`、`generate_iptables_whitelist` 函数签名保持兼容；group 号作为带默认值的参数注入。

## 组件二：采集与启动（容器启动命令）

```bash
mkdir -p /var/log/egress
tcpdump -i nflog:100 -nn -U -w /var/log/egress/allowed.pcap -C 10 -W 5 -Z root &
tcpdump -i nflog:200 -nn -U -w /var/log/egress/denied.pcap  -C 10 -W 5 -Z root &
```

- `-C 10 -W 5`：每份最大 10MB、最多 5 个滚动文件 → 每路 ~50MB 硬上限（两路共 ~100MB）。
- `-U`：包级 flush，读接口可见最新记录。
- `nflog` 接口可任意时刻打开，不依赖 iptables 规则是否已存在；没规则时收不到包，应用策略后自动进数据。tcpdump 可先于 server.py 启动。
- 容器启动 `apt-get install` 那行需加上 `tcpdump`。
- **只有应用过策略（open 或 whitelist）后才会产生 NFLOG 记录**；文档说明。可选：启动脚本里 `curl -XPOST .../policy -d '{"policy_type":"open"}'` 让容器起来即抓（不强制）。
- 目录、`-C/-W`、group 号均走 env，给默认值。

### 配置（`integration/config.py` 新增）

- `HERMES_EGRESS_CAPTURE_DIR`（默认 `/var/log/egress`）
- `HERMES_EGRESS_NFLOG_GROUP_ALLOWED`（默认 `100`）
- `HERMES_EGRESS_NFLOG_GROUP_DENIED`（默认 `200`）
- 采集读取能力随 `egress_policy_enabled()` 一并开启。

## 组件三：读取与解析（新 `capture.py` + `handlers.py`）

### `integration/egress/capture.py`

- **定位文件**：glob `allowed.pcap*` / `denied.pcap*`，按 mtime 排序（轮转后缀不固定，glob 最稳）。
- **解析**：对每个 pcap 跑 `tcpdump -nn -tttt -r <file>`，逐行正则解析为结构化记录。解析不了的行（截断尾包等）跳过，不报错。
- **单条记录字段**：
  - `ts`：绝对时间（`-tttt` 输出，形如 `2026-06-15 11:20:30.123456`）
  - `verdict`：`allowed` / `denied`（由来源 pcap 决定）
  - `proto`：`tcp` / `udp` / 其它
  - `src`、`sport`、`dst`、`dport`
  - `length`：负载长度
  - `flags`：TCP 标志（如有）
- **取最新 N**：从最新文件倒序读，不够再翻上一个文件，凑够 N 即止；全量则全读。两路合并后按 `ts` 倒序。

### 路由（`handlers.py` 的 `try_handle_get` 新增）

```
GET /api/integration/egress/traffic
    ?limit=N      最新 N 条(默认 100)，放行+拒绝合并、时间倒序
    ?all=1        全量(忽略 limit)
    ?verdict=allowed|denied   可选，只返回一类
```

返回：

```json
{
  "ok": true,
  "count": 100,
  "records": [
    {"ts":"2026-06-15 11:20:30.123456","verdict":"denied",
     "proto":"tcp","src":"172.17.0.5","sport":43210,
     "dst":"8.8.8.8","dport":443,"length":0,"flags":"S"}
  ]
}
```

- 路由仅在 `egress_policy_enabled()` 为真时生效，否则 noop（沿用现有 seam 约定）。
- 错误处理沿用现有风格：pcap 目录不存在 / tcpdump 缺失 / 无记录 → `bad(handler, <明确信息>, status=500)` 或返回空列表（无记录返回 `ok:true, count:0, records:[]`，缺工具/目录返回 500）。

## 组件四：测试（`integration/tests/egress/`）

- `test_rules.py`：生成的规则含 `EGRESS_ALLOW` 链、`NFLOG --nflog-group 100/200`、open/whitelist 两形态正确；group 号可配。
- `test_capture.py`：喂固定 `tcpdump -tttt` 文本样例，验证字段解析、verdict 标注、最新 N 截取、两路合并排序、坏行跳过。
- `test_handlers.py`：mock capture 返回，验证路由与参数（limit/all/verdict）、禁用时 noop、错误响应。

## 非目标（YAGNI）

- 不提供 pcap 原始二进制下载接口（如后续需要再加）。
- 不做实时 WebSocket/SSE 推流，仅按需 GET 拉取。
- 不在接口内做 DPI/payload 解析，仅到五元组 + 标志 + 长度。
- 不引入 scapy 等额外 Python 依赖。

## 风险与缓解

- **NFLOG 记录的是连接尝试粒度**（因 ESTABLISHED 先放行），不是逐包全量字节流；满足“出口审计”诉求，若将来要逐包需调整 ctstate 匹配。
- **读取正在写入的 pcap**：末尾可能有半截记录，解析时跳过坏行即可。
- **轮转文件命名**：`-C -W` 后缀因 tcpdump 版本而异，统一用 glob + mtime 排序规避。
- **依赖 tcpdump 存在**：缺失时接口返回明确错误，提示容器需安装 tcpdump。
