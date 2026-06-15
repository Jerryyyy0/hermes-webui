# 出口网关（Egress Gateway）接口文档

- 模块：`integration/egress`
- 能力：用 iptables 控制容器出口流量（全放行 / 白名单），并用 NFLOG + tcpdump 记录出口流量（放行与被拒绝的都记录），通过 HTTP 接口在容器外查询。
- 关联文档：设计 `2026-06-15-egress-traffic-capture-design.md`；测试 `2026-06-15-egress-traffic-capture-test-manual.md`。

---

## 启用条件

接口仅在以下两个环境变量同时满足时生效，否则路由不响应（交还上层，通常 404）：

- `HERMES_INTEGRATION=1`
- `HERMES_EGRESS_POLICY_ENABLED` ∈ {`1`,`true`,`yes`,`on`}

容器还需 `--cap-add=NET_ADMIN --cap-add=NET_RAW`（iptables / NFLOG / tcpdump 所需）。

### 相关环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HERMES_INTEGRATION` | — | 集成总开关，需为 `1` |
| `HERMES_EGRESS_POLICY_ENABLED` | — | egress 能力开关，需为真值 |
| `HERMES_EGRESS_POLICY_RULES_PATH` | `/etc/iptables/rules.v4` | iptables-restore 规则文件落盘路径 |
| `HERMES_EGRESS_CAPTURE_DIR` | `/var/log/egress` | tcpdump 抓包目录（读取接口从此处取 pcap） |
| `HERMES_EGRESS_NFLOG_GROUP_ALLOWED` | `100` | 放行流量的 NFLOG group 号 |
| `HERMES_EGRESS_NFLOG_GROUP_DENIED` | `200` | 被拒绝流量的 NFLOG group 号 |

> 改了 group 号，容器启动命令里的 `tcpdump -i nflog:100/200` 必须同步改。

---

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/integration/egress/policy` | 应用出口策略（全放行 / 白名单） |
| `GET` | `/api/integration/egress/policy` | 查看当前 iptables 规则 |
| `GET` | `/api/integration/egress/traffic` | 读取出口流量记录（最新 N 条 / 全量） |

示例中宿主端口用 `18787`（容器内 `8787`）。

---

## 1. 应用出口策略

`POST /api/integration/egress/policy`

请求体（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `policy_type` | string | 是 | `open`（全放行）或 `whitelist`（默认拒绝 + 白名单放行） |
| `allowed_ips` | string[] | whitelist 必填 | 允许的目的 IP / CIDR，如 `["10.0.0.0/24","1.1.1.1"]` |
| `include_request_ip` | bool | 否 | 为 `true` 时把本次请求来源 IP 也加入白名单 |

行为说明：

- `whitelist`：OUTPUT 默认 DROP；放行回环、已建立/相关连接；白名单目的 IP 经 `EGRESS_ALLOW` 链先 NFLOG（group 100）再 ACCEPT；其余出口包 NFLOG（group 200）后 DROP。
- `open`：全部放行，仅对新出口连接做 NFLOG（group 100）审计。
- 因已建立/相关连接先放行，NFLOG 记录的是**新连接尝试**粒度，非逐包。

### 示例：白名单

```bash
curl -s -X POST http://localhost:18787/api/integration/egress/policy \
  -H 'Content-Type: application/json' \
  -d '{"policy_type":"whitelist","allowed_ips":["10.0.0.0/24"],"include_request_ip":true}'
```

成功响应 `200`：

```json
{
  "ok": true,
  "policy": {
    "policy_type": "whitelist",
    "allowed_ips": ["10.0.0.0/24", "172.17.0.1"],
    "include_request_ip": true
  },
  "rules_path": "/etc/iptables/rules.v4"
}
```

### 示例：全放行

```bash
curl -s -X POST http://localhost:18787/api/integration/egress/policy \
  -H 'Content-Type: application/json' -d '{"policy_type":"open"}'
```

成功响应：`{"ok": true, "policy": {"policy_type": "open"}, "rules_path": "/etc/iptables/rules.v4"}`

### 错误响应

| 状态码 | body | 触发条件 |
|---|---|---|
| `400` | `{"error":"Missing required field(s): policy_type"}` | 缺 `policy_type` |
| `400` | `{"error":"allowed_ips must be a list"}` | whitelist 的 `allowed_ips` 不是数组 |
| `400` | `{"error":"allowed_ips required"}` | 规范化后白名单为空 |
| `400` | `{"error":"invalid ip/cidr: <值>"}` | IP/CIDR 非法 |
| `400` | `{"error":"policy_type must be 'open' or 'whitelist'"}` | 未知 policy_type |
| `500` | `{"ok":false,"error":"<stderr/原因>","policy":{...},"rules_path":"...","returncode":<int>}` | iptables-restore 应用失败 |

---

## 2. 查看当前 iptables 规则

`GET /api/integration/egress/policy`

```bash
curl -s http://localhost:18787/api/integration/egress/policy | jq -r .rules
```

成功响应 `200`：`rules` 为 `iptables -L -n -v` 的文本输出。

```json
{ "ok": true, "rules": "Chain INPUT (policy DROP ...)\n..." }
```

> 用 `-v`（带 in/out 网卡列），避免 `-o lo` 等接口限定规则被误读为“全部放行”。

错误：`500 {"error":"iptables -L failed"}` 或 `{"error":"iptables not found"}`。

---

## 3. 读取出口流量记录

`GET /api/integration/egress/traffic`

查询参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `limit` | int | `100` | 返回最新 N 条；非法值回落 100，负数按 0 |
| `all` | flag | `0` | `1`/`true`/`yes`/`on` 时返回全量，忽略 `limit` |
| `verdict` | string | 空 | 不填返回放行+拒绝；`allowed` 或 `denied` 只返回一类 |

记录按时间倒序（最新在前），放行与拒绝两路合并。

### 示例

```bash
# 最新 50 条（放行 + 拒绝）
curl -s 'http://localhost:18787/api/integration/egress/traffic?limit=50' | jq

# 只看被拒绝的
curl -s 'http://localhost:18787/api/integration/egress/traffic?verdict=denied' | jq

# 全量
curl -s 'http://localhost:18787/api/integration/egress/traffic?all=1' | jq
```

成功响应 `200`：

```json
{
  "ok": true,
  "count": 2,
  "records": [
    {"ts":"2026-06-15 11:20:30.123456","verdict":"denied",
     "proto":"tcp","src":"172.17.0.5","sport":43210,
     "dst":"8.8.8.8","dport":443,"length":0,"flags":"S"},
    {"ts":"2026-06-15 11:20:29.000000","verdict":"allowed",
     "proto":"tcp","src":"172.17.0.5","sport":51020,
     "dst":"1.1.1.1","dport":443,"length":0,"flags":"S"}
  ]
}
```

记录字段：

| 字段 | 说明 |
|---|---|
| `ts` | 抓包时间（`tcpdump -tttt` 本地时间，定宽字符串） |
| `verdict` | `allowed` / `denied`（按来源 pcap 判定，真实 iptables 结果） |
| `proto` | `tcp` / `udp` / `icmp` / `other` |
| `src` / `sport` | 源 IP / 源端口（无端口时为 `null`） |
| `dst` / `dport` | 目的 IP / 目的端口（无端口时为 `null`） |
| `length` | 负载长度 |
| `flags` | TCP 标志（如 `S`、`S.`），非 TCP 为空串 |

### 错误响应

| 状态码 | body | 触发条件 |
|---|---|---|
| `400` | `{"error":"verdict must be 'allowed' or 'denied'"}` | verdict 取值非法 |
| `500` | `{"error":"tcpdump not found in container"}` | 容器未安装 tcpdump |
| `500` | `{"error":"<已脱敏的错误>"}` | 其它读取/解析异常 |

> 未应用过策略前，OUTPUT 链尚无 NFLOG 规则，接口返回 `{"ok":true,"count":0,"records":[]}` 属正常。

---

## docker run 需要新增的内容

在原启动命令基础上新增 **3 处**（其余参数不变）：

1. **capability**（若原来没有）：`--cap-add=NET_ADMIN --cap-add=NET_RAW`
2. **`apt-get install` 行加 `tcpdump`**：`... iptables tcpdump`
3. **`server.py` 之前常驻两个 tcpdump**，并建抓包目录。

`-c` 脚本改为：

```bash
-c "apt-get update && apt-get install -y --no-install-recommends iptables tcpdump \
&& mkdir -p /var/log/egress \
&& ( tcpdump -i nflog:100 -nn -U -w /var/log/egress/allowed.pcap -C 10 -W 5 -Z root & ) \
&& ( tcpdump -i nflog:200 -nn -U -w /var/log/egress/denied.pcap  -C 10 -W 5 -Z root & ) \
&& cd /home/hermeswebui/.hermes/hermes-webui \
&& ( /app/venv/bin/hermes gateway run & ) && sleep 3 && /app/venv/bin/python server.py"
```

抓包参数：`-C 10 -W 5`（每份 10MB、最多 5 个滚动文件，每路 ~50MB 硬上限，两路共 ~100MB）；`-U` 包级 flush 使读接口立即可见；`-Z root` 抓包后保持 root。

> 完整 `docker run` 命令见设计文档「运维：容器启动命令」节。

### 可选环境变量（覆盖默认）

```
-e HERMES_EGRESS_CAPTURE_DIR=/var/log/egress \
-e HERMES_EGRESS_NFLOG_GROUP_ALLOWED=100 \
-e HERMES_EGRESS_NFLOG_GROUP_DENIED=200 \
-e HERMES_EGRESS_POLICY_RULES_PATH=/etc/iptables/rules.v4 \
```

改 group 号时，上面 `tcpdump -i nflog:100/200` 与 `-w` 路径需同步调整。

---

## 注意事项

- **NFLOG 记录只在应用过策略后产生**；tcpdump 先于策略启动也没关系，应用策略后自动开始进数据。
- **HTTP 通、HTTPS 卡** 多为容器出口链路的 MTU/MSS 问题，与本功能无关（iptables 按目的 IP 放行、不分端口）。
- 抓包为**连接尝试**粒度，不是逐包全量字节流。
- pcap 受 `-C/-W` 轮转限制，磁盘占用有硬上限。
