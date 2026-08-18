# Gateway 日志转发到 WebUI 控制台方案

- **状态：** Implemented
- **范围：** Hermes WebUI 在 `server.py` 启动流程中自行拉起的 Profile Gateway 运行日志
- **默认行为：** 默认开启
- **配置变量：** 不新增环境变量；Gateway 输入以 `-vv` 产生完整 stderr 日志，WebUI 自身日志级别仍沿用 `HERMES_WEBUI_LOG_LEVEL`
- **不包含：** 不接管外部已运行或由 systemd/s6/launchd/Docker supervisor 管理的 Gateway；不修改 Hermes Agent 仓库，不把 Gateway 日志写入聊天 SSE，不新增 HTTP API

## 1. 目标

希望在启动 `server.py` 的同一个控制台中，看到由本次 WebUI 启动流程自行管理的 Gateway 日志：

- WebUI 自身日志；
- 每个由 WebUI 自行拉起的 Profile Gateway 日志；
- 这些 Gateway 从启动到退出期间产生的 stdout/stderr。

这里的“WebUI 自己启动”特指 `server.py` 启动时异步调用的
`_ensure_profile_gateways_safe() → ensure_all_profile_gateways()` 路径。不是泛指
机器上所有由 Hermes Agent 启动的 Gateway。

纳入范围的只有：

- WebUI 启动阶段枚举到的 default / 命名 Profile；
- Profile 当前没有运行 Gateway，且 WebUI 成功创建了对应 `Popen` 子进程；
- 该子进程从创建到退出期间写入的 stdout/stderr。

明确不纳入：

- 用户手工执行的 `hermes gateway run`；
- 用户或外部脚本执行的 `hermes gateway start`；
- 已由 systemd、launchd、s6、Docker supervisor 或其他 WebUI 外部进程管理的 Gateway；
- `run_container_services.sh` 启动的 Gateway。

Gateway 日志需要带 Profile 前缀，避免多个 Profile 的日志混在一起时无法判断来源：

```text
[gateway:default] 2026-08-12 12:00:00 INFO gateway started
[gateway:coder]   2026-08-12 12:00:01 WARNING provider retrying
```

## 2. 当前问题

Gateway 不是 WebUI 进程内的普通模块调用，而是独立运行的 Agent 进程。当前 WebUI 通过 `gateway start` 请求 Agent CLI 或外部 service manager 启动它，因此 WebUI 不持有真正的 Gateway 长驻进程，也没有它的 stdout/stderr 管道。

当前链路大致如下：

```text
server.py
  └─ ensure_all_profile_gateways()
       └─ subprocess.run: hermes gateway start
            └─ service manager / detached launcher
                 └─ 独立 Gateway 进程
                      └─ <profile>/logs/gateway.log
```

`server.py` 的 root logger 只能接收当前 WebUI Python 进程内的日志，无法自动接收独立 Gateway 进程的 `logging` 记录。

`gateway start` 当前使用 `subprocess.run(..., capture_output=True)`，只能获取启动命令本身的短暂输出。Gateway 进入后台或由 service manager 接管后，后续运行日志不会通过这个 subprocess 返回给 WebUI。

普通 Docker 容器的路径也不同：

```text
run_container_services.sh
  └─ hermes gateway run
       └─ 重定向到 <profile>/logs/gateway.log
```

因此只修改 WebUI root logger，或者只读取当前 `gateway start` 的 `stdout`，都不能拿到 Gateway 后续运行日志。

本方案不试图覆盖全部 Gateway 来源，只改变 WebUI 自己负责启动的这一条路径，并明确让 WebUI 持有该 Gateway 子进程。

## 3. 设计结论

将非外部托管场景下 WebUI 自己的 Gateway 启动方式从“请求外部启动器”改为“WebUI 直接持有前台 Gateway 子进程”：

```text
server.py
  └─ subprocess.Popen: hermes gateway run
       ├─ stdout=PIPE
       ├─ stderr=STDOUT
       └─ GatewayLogReader
            └─ integration.project_logging
                 └─ server.py 控制台 / WebUI 运行日志
```

进程管理器遵守以下规则：

1. 默认开启，不增加新的环境变量。
2. 只对本次 `server.py` 成功创建并持有的 Gateway 子进程启用直连。
3. Gateway 的 stdout/stderr 使用管道实时读取，不依赖 `gateway.log` 轮询。
4. Gateway 转发行使用专用 console sink 输出，默认不受 `HERMES_WEBUI_LOG_LEVEL` 过滤；WebUI 自身日志仍沿用该级别。
5. 已运行 Gateway 或外部 supervisor 管理的 Gateway 不被 WebUI 接管，也不在本方案范围内。
6. WebUI 退出时停止自己创建的 Gateway 子进程、等待 reader 线程退出并关闭管道。

实现前先确定 Gateway 所有权：只有当前 Profile 的权威状态为 `not_running`、当前环境不是 s6/容器外部 supervisor、且本次 `Popen` 由 WebUI 成功创建的进程，才标记为 WebUI-owned 并接入 reader。s6、`run_container_services.sh` 或其他 supervisor 路径继续保留其现有托管方式，不强行改成 WebUI pipe。

## 4. 模块与文件改动

### 4.1 新增 `integration/gateway_startup/process.py`

新增 Fork-owned 模块，负责 WebUI-owned Gateway 的启动、stdout/stderr 读取、退出监控和停止清理。敏感文本清理放在同一 feature 下的 `integration/gateway_startup/redaction.py`，不散落到 reader 或全局 logger。

实现提供以下内部能力：

```python
start_gateway_process(profile) -> GatewayProcess
stop_gateway_processes() -> None
```

每个 Profile 行至少使用以下字段：

```python
{
    "name": "default",
    "path": "/path/to/profile",
}
```

运行命令由现有 `AgentCliInvocation` 和 Profile 信息构建，但 action 从 `start` 改为前台 `run`。固定使用 `gateway run -vv --external-supervisor`：`-vv` 让 Agent Gateway 将 DEBUG 级别日志写入 stderr；`--external-supervisor` 让 Agent 的计划重启退出回 WebUI，而不是自行派生脱离管道的新进程。**不得使用 `--force` 或 `--replace`**；它们会绕过或破坏 Agent 的外部 supervisor、multiplex 和单实例保护。必须继续使用已验证的 Agent runtime、cwd 和 env，不通过任意 `PATH` 或拼接 shell 字符串启动。

`--external-supervisor` 会让 Agent 跳过其面向交互 shell 的部分重复实例预检查，因此 WebUI 在使用该参数前必须完成下文定义的权威三态探测；Agent 启动阶段的 PID file/runtime lock 仍负责拒绝任何探测后的竞态重复实例。

不要使用 `shell=True`。不要把 stdout/stderr 重定向到 `DEVNULL`。建议使用 `stderr=subprocess.STDOUT` 合并成一个有序日志流，避免两个 reader 线程导致 stdout/stderr 行序不确定。

Profile 运行环境必须来自已解析的 runtime/profile 对，而不是在 reader 线程中重新读取当前进程的 `HERMES_HOME`；避免 Profile 切换或环境变量变化影响已经创建的子进程。

### 4.2 修改 `integration/gateway_startup/__init__.py`

导出 Gateway 进程启动和停止入口，保持 `server.py` 只依赖 integration 层的薄接口。

### 4.3 修改 `integration/gateway_startup/runtime.py`

保留现有 `gateway start` 命令构造能力，新增前台运行命令的构造入口，或让现有 builder 支持受控的额外参数。调用方只能传入固定的 action/参数集合，不能把任意命令行参数透传到 `Popen`。

前台命令的最终形态应明确为：

```text
<validated-agent-cli> gateway run -vv --external-supervisor
```

其中 runtime 解析出的 CLI 前缀、Profile cwd 和环境变量必须与现有 WebUI Gateway 启动路径保持一致；`-vv` 和 `--external-supervisor` 是 WebUI-owned Gateway 的固定策略，不新增用户配置项。

### 4.4 修改 `integration/gateway_startup/startup.py` 与 `server.py`

`server.py` 继续通过现有的 `_ensure_profile_gateways_safe()` 启动协调入口调用 `ensure_all_profile_gateways()`；实际变更集中在 `integration/gateway_startup/startup.py` 的 `_start_profile_gateway()` 及其结果汇总逻辑：将现有 `subprocess.run(... gateway start ...)` 改为调用进程管理器执行 `subprocess.Popen(... gateway run -vv --external-supervisor ...)`，并把返回的进程句柄登记到 integration-owned registry。

`Popen` 创建成功即视为“启动请求已提交”，不能等待 Gateway 进程结束，也不能复用当前 `subprocess.run` 的 timeout/CompletedProcess 语义。启动汇总结果应区分“已创建进程”和“启动失败”；进程后续立即退出由 reader/exit watcher 记录，不阻塞 `server.py` 启动线程。

`startup.py` 保留现有的 Profile 去重和 multiplex 选择逻辑，只替换“启动动作”的实现。测试注入点从 `runner=subprocess.run` 调整为可注入的 process factory / runtime resolver，使启动结果、reader 和 shutdown 可以在不拉起真实 Agent 的情况下验证。

现有 `list_profiles_api()` 在 Gateway 状态探测异常时会把 `gateway_running` 降级为 `False`，不能把该值直接当作“确认未运行”。进程管理器需新增仅供启动协调使用的 `probe_profile_gateway_state(profile)`，基于目标 Profile 的确切 Agent runtime/PID file/runtime lock 返回三态结果；不能解析面向人的 `gateway status` 文本，也不能复用 UI 列表的降级布尔值：

- `running`：跳过，不创建子进程；
- `not_running`：才允许执行 `Popen`；
- `unknown`：失败关闭，记录 warning，不启动。

即使探测为 `not_running`，Agent `gateway run` 自己的 PID/runtime lock 仍是处理检查到启动之间竞态的最后一道保护；命令因锁冲突退出时，WebUI 只记录失败、清理 reader/registry，不重试或抢占。

启动协调器需要保留外部托管分支：检测到 s6 或其他既有 supervisor 所有权时，不创建 WebUI-owned `Popen`、不登记 process registry、不开 reader；该分支是否继续调用既有 `gateway start` 由现有容器/服务契约决定，但其日志不在本方案内。这样“server.py 触发了启动协调”与“WebUI 持有 Gateway 进程”不会被混为一谈。

每个 WebUI-owned Gateway 至少需要三个生命周期状态：

- `starting`：Popen 已创建，reader 已启动，等待 Gateway 进入运行态；
- `running`：持续读取 stdout/stderr，并保留进程状态；
- `exited`：reader 排空剩余输出、记录退出码并释放句柄。

如果 Profile 的权威状态为 `running`，跳过，不发送 `--force` / `--replace`，不杀掉或接管现有 Gateway。

如果现有的 `HERMES_WEBUI_START_PROFILE_GATEWAYS=0` 被设置，WebUI 不创建 Gateway，也不创建 stdout/stderr reader。这是“未由 WebUI 启动”的路径，不需要补偿性读取；本方案不新增任何控制变量。

普通 Docker 容器中 WebUI 当前会跳过 Gateway 启动协调器，Gateway 由 `run_container_services.sh` 管理；该 Gateway 不属于本方案范围。只有 WebUI 实际执行 `Popen` 并持有句柄的进程，才进入本方案的 registry 和日志 reader。

在 `server.py` 的 `serve_forever()` 清理路径中调用 `stop_gateway_processes()`，保证关闭顺序如下：

```text
停止 HTTP 服务
  → 停止 WebUI-owned Gateway 进程
  → 等待读取线程排空 stdout/stderr
  → 停止 WebUI watcher / drain / reaper
  → 完成其他 WebUI shutdown cleanup
```

### 4.5 修改 `integration/project_logging/config.py` 与 `__init__.py`

新增仅供 Gateway 转发使用的 `log_gateway_line(level, line)` 内部 console sink。它复用当前 stderr/tee 与时间格式，但不受 `HERMES_WEBUI_LOG_LEVEL` 阈值过滤；不得改变普通 WebUI logger 的现有等级语义。

### 4.6 修改 `integration/README.md`

补充 Gateway 控制台日志行为：

- 默认开启；
- 日志来源是 WebUI-owned Gateway 子进程的 stdout/stderr；
- 多 Profile 使用 `[gateway:<profile>]` 前缀；
- `HERMES_WEBUI_LOG_LEVEL` 仍控制 WebUI 自身日志；Gateway 转发行默认完整显示；
- 不接管已经运行的 Gateway，也不读取外部 Gateway 的历史日志；
- 日志转发不会写入聊天流。

### 4.7 修改 `integration/CHANGELOG.md`

在 Fork 集成层的 `[Unreleased]` 中记录该用户可见行为变化。根目录 `CHANGELOG.md` 不修改。

## 5. WebUI-owned Gateway 进程行为

### 5.1 子进程启动与读取

WebUI 创建 `Popen` 后立即启动 reader。reader 不读取历史文件，只读取子进程从启动后写入管道的内容。

如果 Gateway 尚未写出任何内容，reader 阻塞在管道读取上，不进行文件轮询；Gateway 启动或运行过程中写入的 stdout/stderr 会被立即消费。

### 5.1.1 实时性目标与边界

本方案承诺的是**近实时转发**，不是零延迟，也不是对 Gateway 尚未落盘内容的实时监听。

实时性链路为：

```text
Gateway logging / print 调用
  → Gateway stdout/stderr flush
  → WebUI pipe reader
  → WebUI logger handler 输出
  → server.py 控制台
```

实现目标：

- reader 启动必须发生在 `Popen` 成功后，不等待额外轮询周期；
- 在 Gateway stdout/stderr 已 flush 到管道的前提下，目标是**几十毫秒到 100ms 级别**内转发到 WebUI 控制台；
- reader 使用独立阻塞字节读取线程，不采用带超时轮询的 `readline()`；该线程按 chunk 拆行，并对无换行超长内容执行明确的截断/丢弃策略；
- shutdown 使用 stop event 加进程终止/等待，不依赖日志文件状态。

上述时间是 WebUI 侧的目标，不是端到端硬保证。以下延迟不由 WebUI 控制：

- Gateway logger 没有及时 flush；
- Gateway 自己的日志级别或 quiet/verbosity 设置过滤掉了输出；
- 子进程内部或操作系统对 pipe 写入进行了缓冲；
- 操作系统调度、磁盘阻塞或进程资源耗尽。

因此文档和测试中使用以下准确表述：

> WebUI 自己启动的 Gateway 日志在已经 flush 到 stdout/stderr pipe 后，通常会在几十毫秒到 100ms 级别内转发到控制台；不承诺零延迟，也不负责补回 Gateway 尚未写入 pipe 的内容。

### 5.2 新增行

读取逻辑必须支持：

- 一次追加多行；
- 最后一行暂时没有换行符；
- Gateway 正在写入时的部分行；
- UTF-8 解码异常时使用替换字符而不是终止追踪线程。

常规情况下，只有读到完整换行的内容才作为一条日志记录输出；部分行保留在内存 buffer 中，等待下一次读取补全。

必须设置 `_MAX_PENDING_LINE_BYTES`（建议 64 KiB）。当无换行 buffer 达到该上限时，立即转发已截断部分并标记 `truncated=true`，进入“丢弃到下一个换行”状态；后续字节不再累积，直到读到换行才恢复正常拆行。这样 Gateway 或第三方库输出二进制/超长进度内容时，不会无界占用内存，也不会永久阻塞后续日志。

读取循环必须持续消费 pipe，直到达到以下任一条件：

- 暂时读不到更多字节；
- 达到单次最大读取预算；
- stop event 已设置。

达到单次最大读取预算后，reader 应立即继续读取，不能丢弃剩余内容。这样既能降低正常日志的延迟，也能避免 Gateway 短时间大量输出时单次占用过多 CPU 或内存。

如果 pipe 最后没有换行符，尾部内容只保存在该进程对应的 buffer 中。Gateway 正常退出后，如果仍有非空尾部，应作为一条带 `unterminated=true` 的诊断行转发，避免退出时静默丢失最后一条日志。

正常的 Python logging 通常会以带换行的完整记录写入，因此部分行规则主要用于防止并发写入、进程中断和外部日志重定向造成的半行污染。

### 5.2.1 Pipe 读取策略

使用 Python 标准库 pipe 读取，不引入文件监控第三方依赖。stdout/stderr pipe 本身提供阻塞唤醒，比轮询 `gateway.log` 更适合本方案的 WebUI-owned 子进程。

建议常量：

```python
_READ_CHUNK_BYTES = 64 * 1024
_MAX_BYTES_PER_READ_BATCH = 1024 * 1024
_MAX_PENDING_LINE_BYTES = 64 * 1024
```

这些是实现内部常量，不暴露为新的环境变量或用户配置项。`Popen` 必须以二进制、无父进程文本缓冲方式创建；每个 Gateway 使用一个 daemon reader 线程，通过 `os.read(pipe.fileno(), _READ_CHUNK_BYTES)`（或等价未缓冲原始 pipe read）阻塞读取。不能使用可能等待填满 Python 用户态 buffer 的 `BufferedReader.read(n)`，否则低频日志会被人为延迟。

reader 使用 `codecs.getincrementaldecoder("utf-8")("replace")` 在 chunk 边界持续解码，EOF 时以 `final=True` 刷出尾部，随后才做行拆分和转发。这样 UTF-8 多字节字符跨两个 pipe chunk 时不会被误替换或截断。读取到数据后按 chunk 拆行、在单批预算后让出执行权并立即继续读取；不得依赖 `select()`、文件 mtime 或定时 sleep 来获得“实时性”。

### 5.3 子进程退出、计划重启与异常退出

reader 读到 EOF 后必须先等待并收集子进程退出状态，再按以下规则处理：

- 正常退出：记录 Profile、PID 和 exit code；
- 非零退出：记录 warning/error；
- WebUI shutdown 触发的退出：标记为 expected，避免误报 Gateway 崩溃；
- 子进程退出前已经写入 pipe 的尾部内容必须先排空，再关闭 pipe。

默认不因崩溃、启动失败或非零退出自动重启；这类退出记录后保持 `exited`，避免隐藏配置错误或形成重启风暴。

但 Agent 的聊天内 `/restart`、更新等**计划重启**不能走其默认 detached restart watcher：新进程会脱离 WebUI，stdout/stderr pipe 也随之丢失。因此 WebUI-owned Gateway 启动时必须附带 Agent 已有的 `--external-supervisor` 契约，使 Agent 在计划重启时只完成当前进程的有序退出、不会自行派生 replacement。此参数仅声明 WebUI 是该前台子进程的重启协调者，不等同于 `--force`，也不会绕过外部 Gateway 的冲突保护。

进程 manager 仅在同时满足以下条件时重新拉起一次同 Profile Gateway：

1. 旧进程是 registry 中仍标记为 WebUI-owned 的进程；
2. WebUI 未进入 shutdown；
3. 退出被可靠识别为 Agent 的计划重启（例如 exit code `75` 或 Agent 已持久化的 `restart_requested=true` 状态）；
4. 重新执行权威状态探测仍得到 `not_running`。

重新拉起必须走与首次启动相同的 `Popen(... gateway run -vv --external-supervisor ...)`、新 pipe 和新 reader，并替换 registry 句柄。计划重启检测不可靠、重新探测为 `running/unknown`、或 replacement 启动失败时，均不重试、不接管外部进程，只记录受控 warning。

### 5.4 多 Profile 与去重

按 WebUI-owned 子进程的唯一句柄和 Profile 名称管理：

- 同一个 `Popen` 只创建一个 reader；
- Profile 名称只用于显示前缀；
- Profile 列表重复不会重复启动同一个 Profile；
- 已存在的 Gateway 不创建 reader，也不加入 registry。

建议将 Profile 名称、PID、Popen 句柄和 reader 状态保存在 process registry 中，便于日志行格式化、退出审计和 shutdown 清理。

## 6. 日志级别与格式

Gateway 子进程固定以 `-vv` 运行，确保 Agent 默认会把 `DEBUG` 及以上日志写入 stderr。为了满足“WebUI 自己启动的 Gateway 日志都能在控制台看到”，Gateway 转发行**不能**再经过 `HERMES_WEBUI_LOG_LEVEL` 过滤；该变量继续只控制 WebUI 自身的常规 logger。

应在 `integration.project_logging` 增加一个内部、专用的 console sink（例如 `log_gateway_line`）：它复用当前 stderr/tee 目的地和时间格式，但不受项目 logger level 阈值影响，也不改变其他模块的日志策略。不能用裸 `print()`，避免绕开现有 stderr/tee 生命周期。

Reader 从 Gateway 输出行中识别标准级别：

```text
DEBUG / INFO / WARNING / WARN / ERROR / CRITICAL
```

识别不到级别时按 `INFO` 处理。

转发时调用专用 sink，而不是通用 logger 或直接 `print()`：

```text
log_gateway_line(level, "[gateway:%s] %s", profile_name, line)
```

这样可以复用：

- WebUI 控制台 handler；
- WebUI 运行日志文件；
- 现有日志时间格式。

`level` 仍用于展示（例如 ERROR/WARNING），但不能作为 Gateway console sink 的过滤条件。只有 Gateway 实际写入 stdout/stderr pipe 的内容才可转发；本方案不使用 quiet，且 `-vv` 覆盖 Agent CLI 的默认 WARNING stderr 阈值。

## 7. 安全与稳定性约束

### 7.1 进程启动边界

命令只能由已验证的 Agent runtime、Profile 和固定的 `gateway run -vv --external-supervisor` 参数构造，不接受浏览器传入命令或路径，不使用 `shell=True`。

启动前必须使用 4.4 定义的权威三态探测，而不是 UI 行上的降级 `gateway_running` 布尔值：

- `running`：跳过，不发送 `--force` / `--replace`，不接管现有 Gateway；
- `not_running`：WebUI 才允许创建自己的前台 Gateway 子进程；
- 状态未知或 Profile 信息不完整：失败关闭，不启动可能重复的 Gateway。

绝不传 `--force` 或 `--replace`。前者会绕过外部 service/multiplex 冲突保护，后者会终止现有 Gateway；二者都与本方案“只管理自己创建的进程”的范围冲突。`--external-supervisor` 仅用来禁止 Agent 在计划重启时私自派生 replacement；它不授予抢占权限。

若外部 Gateway 在探测后、Popen 前抢先启动，Agent 的 PID/runtime lock 检查失败时，WebUI 应记录失败并释放自己的子进程，不重试抢占。

### 7.2 控制台输出清理

转发前需要：

- 移除 ANSI 控制序列和不可见控制字符；
- 将换行规范化为单行；
- 对单行设置长度上限，避免异常日志一次占满内存或终端；
- UTF-8 解码失败时替换非法字节。

Gateway 标准 logger 的文件输出仍由 Agent 自己的 formatter 负责。WebUI 在 reader 边界必须调用新增的本地 `integration.gateway_startup.redaction.redact_gateway_console_line(line)`，再调用 WebUI 的 `one_line(..., max_len=...)` 和 ANSI/control-character 清理。该 helper 不能依赖从 Agent runtime 直接 import：WebUI 和 Gateway CLI 可能来自不同 venv/Python 环境。

该本地 helper 必须始终严格脱敏，不受 Profile 或 Agent 的日志脱敏开关影响；至少覆盖已知 token 前缀、`Authorization` / `Proxy-Authorization`、Cookie / `Set-Cookie`、`x-api-key` / `api-key`、`KEY=value` 和 YAML/JSON secret 字段、JWT、URL userinfo 及 query 中 credential 命名参数。它可以复用或移植 Agent `agent.redact.redact_sensitive_text(..., force=True, redact_url_credentials=True)` 的规则和测试样例，但不能运行时依赖 Agent 包可导入。

该顺序必须是“解码 → 去 ANSI/control → 本地强制脱敏 → 单行长度限制 → 输出”。测试要覆盖 `Authorization: Bearer ...`、Cookie、`OPENAI_API_KEY=...`、`x-api-key: ...`、URL query token，以及已脱敏日志不被二次破坏的情况。

### 7.3 不影响 WebUI 请求线程

pipe 读取和进程等待必须运行在 daemon 后台线程，不得在 HTTP 请求线程、SSE 线程或 WebUI 主启动线程中阻塞等待日志或 Gateway 退出。计划重启的 replacement 调度也必须在 process manager worker 中串行完成，不能从 Agent 输出内容或 HTTP 请求直接触发。

单次读取设置最大字节数，并执行 5.2 定义的 `_MAX_PENDING_LINE_BYTES` 截断/丢弃状态机，避免 Gateway 一次写入超大无换行内容时阻塞或造成内存放大。

单次读取预算只限制一次 reader 批次的工作量，不限制最终读取结果。达到预算后应立即继续排空；实现不得静默丢弃超过预算的内容。

### 7.4 不让日志故障影响服务

以下情况只记录一次受控 warning，然后继续运行 WebUI：

- Gateway 命令启动失败；
- stdout/stderr pipe 打开失败；
- pipe 解码失败；
- Gateway 非零退出；
- 单行解码失败。

Process/reader 自身异常必须在 worker 边界被捕获，不能让后台线程异常退出后无人知晓，也不能让异常传播到 HTTP 服务。

## 8. 状态所有权与生命周期

| 状态 | 所有者 | 创建 | 清理 |
| --- | --- | --- | --- |
| Profile → Gateway process | Gateway process manager | WebUI 启动阶段枚举 Profile | 子进程退出或 WebUI shutdown |
| Popen 句柄 | 单个 WebUI-owned process | `Popen` 成功后 | reader 排空后关闭 |
| stdout/stderr pipe | 单个 reader worker | 子进程创建时 | EOF、错误或 shutdown |
| 部分行 buffer | 单个 reader worker | 读到无换行尾部时 | 补全、进程退出或 shutdown |
| process stop event | process manager | 首次创建 WebUI-owned process | `stop_gateway_processes()` |
| 计划重启 replacement 标记 | process manager | 确认 WebUI-owned 进程计划退出时 | replacement 启动结束或 shutdown |

必须保证启动成功、启动失败、pipe 错误、Gateway 退出、WebUI SIGTERM 和重复启动路径都能释放进程句柄、pipe 和 reader worker。

### 8.1 shutdown 与实时性

`stop_gateway_processes()` 的清理顺序必须是（接在 `server.py` 的 `httpd.server_close()` 后、现有 watcher/drain 清理前）：

1. 设置 manager-level stop event；
2. 对 WebUI-owned Gateway 发送温和终止信号；
3. 等待 reader 排空 pipe 和进程在有界超时内退出；
4. 超时后按平台安全地终止残留子进程；
5. 关闭各自 pipe 句柄；
6. 清空 manager registry。

为避免 Gateway 内部派生进程在 WebUI 退出后遗留，进程管理器应按平台创建可识别的 process group/session，并在超时清理时终止该 WebUI-owned group；不能扫描并终止不属于 registry 的同名或其他 Profile 进程。

不能在 shutdown 时无限等待 Gateway 或等待一个永远不再写入的半行。超过有界等待时间后，记录一次 WebUI warning 并放弃未完成尾部；这属于进程退出时的可接受边界，不得阻塞 HTTP 服务关闭。

## 9. 测试计划

新增或更新：

```text
integration/tests/gateway_startup/test_process.py
integration/tests/gateway_startup/test_startup.py
```

至少覆盖：

1. Popen 使用 `gateway run -vv --external-supervisor`，而不是 `gateway start`，且绝不传 `--force` / `--replace`。
2. stdout/stderr 被接入 reader，输出能进入 Gateway console sink。
3. 多 Profile 日志带正确的 Profile 前缀。
4. `DEBUG`、`INFO`、`WARNING`、`ERROR` 级别映射正确，且即使 `HERMES_WEBUI_LOG_LEVEL=INFO/WARNING`，Gateway DEBUG 转发行仍能进入控制台专用 sink。
5. reader 不读取或回放任何历史 `gateway.log` 内容。
6. 不完整行会等待补全；进程退出时尾部不会静默丢失。
7. 权威状态为 `running` 的 Gateway 会被跳过，不发送 `--force` / `--replace`。
8. UI 列表中的 `gateway_running=False` 但权威探测失败时不会启动，满足失败关闭。
9. 相同 Profile 不会重复创建 WebUI-owned Gateway。
10. Gateway 非零退出会被记录，但不会自动重启。
11. 停止流程会终止 WebUI-owned Gateway，排空 pipe 并释放句柄。
12. Agent 计划重启（exit code 75 / `restart_requested=true`）只重启原 registry 中的 WebUI-owned Profile，并以新 pipe/new reader 重新接入；非计划退出、状态 `running/unknown` 与 replacement 失败均不重试。
13. `Authorization`、Cookie、API key、URL query token 均在 Gateway console sink 前被强制脱敏。
14. 超长无换行输出在 `_MAX_PENDING_LINE_BYTES` 后被截断并丢弃到下一个换行，buffer 不会无限增长。
15. 测试验证 reader 是阻塞 pipe 消费、无定时轮询；不把几十毫秒到 100ms 写成 CI 的硬实时上限。
16. 连续输出多行时，reader 会在一次可用数据批次中排空，不会人为每行增加等待周期。
17. WebUI 运行在不同于 Agent 的 Python/venv 时，本地脱敏 helper 仍可工作，不需要 import Agent 包。
18. UTF-8 多字节字符跨 pipe chunk 时仍能正确还原；reader 使用未缓冲原始 pipe read，不因 Python 用户态 buffer 等待而延迟低频日志。

邻近验证：

```bash
./scripts/test.sh integration/tests/gateway_startup tests/test_server_gateway_startup.py
```

手动验证需要覆盖：

- 默认 Profile Gateway；
- 至少一个命名 Profile Gateway；
- Gateway 进程启动、运行中输出和正常退出；
- Gateway 运行中产生一条 `INFO` 和一条 `ERROR`；
- Gateway 非零退出；
- Gateway 聊天内计划重启后，replacement 的日志仍显示在同一个 WebUI 控制台；
- 已有 Gateway 正在运行时 WebUI 不接管；
- WebUI SIGTERM 后确认没有 WebUI-owned Gateway 或 reader 线程残留。

## 10. 明确不做的事情

- 不新增 `HERMES_WEBUI_GATEWAY_CONSOLE_LOG` 等环境变量，默认始终启用。
- 不修改 Hermes Agent 的 `hermes_logging.py`。
- 不把 Gateway 日志转成浏览器 SSE 事件。
- 不接管已经运行或由外部 supervisor 管理的 Gateway。
- 不读取 `gateway.log` 作为本方案的日志来源。
- 不通过 `tail -f` 子进程实现，避免重复引入一个日志进程和独立生命周期。
- 不把 Gateway stdout/stderr 写入 Session transcript 或 Agent 上下文。
- 不保证 Gateway 尚未写入 stdout/stderr pipe 的内容能够被 WebUI 恢复。
- 不保证 Gateway 调用 logger 到 WebUI 控制台之间零延迟；可观察到的延迟以 Gateway flush、pipe 可见性和 WebUI reader 调度为边界。
