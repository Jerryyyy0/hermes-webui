# Egress 流量记录 测试手册

- 日期：2026-06-15
- 适用：`integration/egress` 出口流量记录与读取接口（NFLOG + tcpdump + `/traffic`）
- 关联文档：`2026-06-15-egress-traffic-capture-design.md`（设计与运维启动命令）

## 约定

- 容器名：`zhiling-user-user-zrf`
- API 端口：宿主 `18787` → 容器内 `8787`
- NFLOG group：放行 `100` / 拒绝 `200`；抓包目录 `/var/log/egress`
- 标记：**🖥️【服务器/宿主机】** 与 **📦【容器内】**

> 容器内命令两种跑法：先 `docker exec -it zhiling-user-user-zrf bash` 进入再执行；或在宿主机直接 `docker exec zhiling-user-user-zrf <命令>`。下文 📦 块即“语义上在容器内执行”。

---

## 0. 启动容器 🖥️

使用设计文档「运维：容器启动命令」节的完整命令启动（关键增量：`apt-get ... tcpdump`、`mkdir -p /var/log/egress`、两条常驻 tcpdump）。启动后确认 server 就绪：

```bash
docker logs -f zhiling-user-user-zrf
# 看到 server.py 正常监听后 Ctrl-C 退出日志查看（容器继续运行）
```

## 1. 确认 tcpdump 常驻、抓包目录就绪 📦

```bash
docker exec zhiling-user-user-zrf sh -c 'ps aux | grep -E "tcpdump.*nflog" | grep -v grep'
docker exec zhiling-user-user-zrf ls -la /var/log/egress/
```

预期：可见 `tcpdump -i nflog:100` 与 `nflog:200` 两个进程；目录此时可能尚无 pcap（有流量后才生成）。

## 2. 验证“空”状态（尚未应用策略）🖥️

```bash
curl -s 'http://localhost:18787/api/integration/egress/traffic?limit=20' | jq
```

预期：`{"ok": true, "count": 0, "records": []}`（无策略 → 无 NFLOG 规则 → 无记录）。

## 3. 应用白名单策略 🖥️

只放行 `1.1.1.1`，并把请求来源 IP 一并加入白名单：

```bash
curl -s -X POST http://localhost:18787/api/integration/egress/policy \
  -H 'Content-Type: application/json' \
  -d '{"policy_type":"whitelist","allowed_ips":["1.1.1.1/32"],"include_request_ip":true}' | jq
```

预期：`"ok": true`，`policy.allowed_ips` 含 `1.1.1.1/32` 与来源 IP。

## 4. 确认 iptables 已插入 NFLOG 规则 📦

```bash
docker exec zhiling-user-user-zrf iptables -nL OUTPUT -v
docker exec zhiling-user-user-zrf iptables -nL EGRESS_ALLOW -v
```

预期 OUTPUT 链含：跳转 `EGRESS_ALLOW`、`NFLOG ... nflog-group 100`，末尾 `NFLOG ... nflog-group 200` 再 `DROP`。

## 5. 制造流量（放行 + 拒绝各一条）📦

用 **IP 直连**避免 DNS 干扰。`1.1.1.1` 在白名单应成功；`8.8.8.8` 不在应超时被拒：

```bash
# 放行：应能连上（记 allowed / group 100）
docker exec zhiling-user-user-zrf curl -k -s -o /dev/null -w "allowed test -> %{http_code}\n" --connect-timeout 5 https://1.1.1.1

# 拒绝：应超时失败（记 denied / group 200）
docker exec zhiling-user-user-zrf curl -k -s -o /dev/null -w "denied test -> %{http_code}\n" --connect-timeout 5 https://8.8.8.8
```

预期：第一条返回某 HTTP 状态码（连通）；第二条卡约 5 秒后失败（`000`/超时）。

> 验证 ICMP（无端口 IP 解析）：`docker exec zhiling-user-user-zrf ping -c 2 -W 2 8.8.8.8`（被拒，记 denied）。

## 6. 读取并验证流量日志 🖥️

```bash
# 最新记录（放行+拒绝合并，时间倒序）
curl -s 'http://localhost:18787/api/integration/egress/traffic?limit=50' | jq

# 只看被拒绝的
curl -s 'http://localhost:18787/api/integration/egress/traffic?verdict=denied' | jq

# 只看放行的
curl -s 'http://localhost:18787/api/integration/egress/traffic?verdict=allowed' | jq

# 全量（仅看条数）
curl -s 'http://localhost:18787/api/integration/egress/traffic?all=1' | jq '.count'
```

预期：能看到 `"dst":"8.8.8.8"` 且 `"verdict":"denied"`，以及 `"dst":"1.1.1.1"` 且 `"verdict":"allowed"` 的记录；每条含 `ts/verdict/proto/src/sport/dst/dport/length/flags`。

## 7. 文件层面佐证（可选）📦

```bash
docker exec zhiling-user-user-zrf ls -la /var/log/egress/
docker exec zhiling-user-user-zrf sh -c 'tcpdump -nn -tttt -r /var/log/egress/denied.pcap 2>/dev/null | tail'
```

预期：`allowed.pcap` / `denied.pcap` 存在并增长；`-r` 能看到对 `8.8.8.8` 的 SYN。

## 8. 边界/异常测试 🖥️

```bash
# 非法 verdict -> 400
curl -s -o /dev/null -w "%{http_code}\n" 'http://localhost:18787/api/integration/egress/traffic?verdict=bogus'   # 期望 400

# limit 非法 -> 回落默认 100，不报错
curl -s 'http://localhost:18787/api/integration/egress/traffic?limit=abc' | jq '.ok'                              # 期望 true
```

---

## 排错对照

| 现象 | 原因 / 排查 |
|---|---|
| `traffic` 一直 `count:0` | 还没 `POST /policy`（步骤 3）；或 tcpdump 没起来（步骤 1）；或确实没有新连接产生 |
| 接口 500 `tcpdump not found` | 容器未装 tcpdump，检查 `apt-get install` 那行 |
| `iptables -nL` 无 NFLOG | 策略未应用成功，回看步骤 3 返回；确认 `HERMES_EGRESS_POLICY_ENABLED=1` |
| denied 测试反而连通 | 目标 IP 命中白名单/来源 IP；换一个确定不在白名单的 IP |
| tcpdump 进程不存在 | 看 `docker logs`；确认 `--cap-add=NET_ADMIN --cap-add=NET_RAW`，以及内核/libpcap 支持 nflog |
| 应用白名单后容器其它功能断网 | 正常——白名单会 DROP 非白名单出口；测完用下方命令恢复 |

**恢复全放行**（测试结束）🖥️：

```bash
curl -s -X POST http://localhost:18787/api/integration/egress/policy \
  -H 'Content-Type: application/json' -d '{"policy_type":"open"}' | jq
```

---

## 一句话流程

🖥️ 启动 → 📦 确认 tcpdump → 🖥️ POST 白名单 → 📦 确认 iptables NFLOG → 📦 curl `1.1.1.1` 与 `8.8.8.8` 制造流量 → 🖥️ GET `/traffic` 验证 verdict。
