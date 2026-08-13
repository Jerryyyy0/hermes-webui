# `terminal(background=true)` 运行期完成通知接缝改造方案

- **状态：** Proposed
- **范围：** 单次 Hermes Agent 与 WebUI 进程生命周期内的后台 terminal 通知、路由和展示投影
- **不包含：** 服务重启后的补投、事件 outbox、delivery sidecar、跨重启 turn 关联

## 1. 结论与改造目标

后台进程能力和 WebUI completion queue drain 已存在，但当前接缝有两个独立问题：通知配置在进程启动后才写入，极快任务可能漏掉完成事件；普通 `process_wakeup` 仍可能被当作新的用户输入展示。

本方案把“是否通知、归属哪个会话、如何展示”作为一条运行期状态链。状态在产生处一次确定，再由下游只读消费；不为重启恢复额外保存事件。

目标如下：

1. 已传 `notify_on_complete=true` 的后台任务，即使瞬间退出，也恰好产生一次 completion event。
2. completion event 有不可变的 WebUI 会话归属；不能凭最近 user、命令文本或进程 id 前缀猜测。
3. 唤醒提示可进入模型上下文和 durable transcript，但一定标为 `context_anchor`，不展示为普通 user 气泡。
4. 后续真实 assistant 结果保持时间线尾部顺序，并在 WebUI 展示/持久化副本中携带发起该进程的 `origin_turn_key`；进程完成不会伪造一轮人类提问，也不会把迟到结果物理插回旧位置。
5. 默认保持“显式通知”：只有模型显式传 `notify_on_complete=true` 或 `watch_patterns` 才承诺异步回灌。不得把所有后台服务自动改为完成通知。

服务重启是明确接受的边界：Agent 或 WebUI 重启后，未处理的内存 event 和 deferred wakeup 可能丢失。系统只记录诊断，不猜测归属、不补造 user turn。

## 2. 当前链路与根因

当前生产链路是：`terminal_tool()` 创建 `ProcessSession` 并启动 reader/poller；随后才将 `notify_on_complete` 和 `watch_patterns` 回填到该 session；reader/poller 退出时由 `_move_to_finished()` 根据 session 上的字段决定是否向 `completion_queue` 投递。

```text
terminal_tool
  ├─ spawn_local / spawn_via_env
  │    └─ reader 或 poller 可能立即完成
  │         └─ _move_to_finished(): if session.notify_on_complete -> enqueue
  └─ proc_session.notify_on_complete = True     ← 当前写入点过晚
```

因此 `true` 参数并不是从启动到结束全程有效。一个极快的 `pytest -q`、语法检查或立即失败的 shell 命令，可能先结束、后写标记，导致完成通知丢失。

WebUI 收到 event 后会在空闲会话发起 `source=process_wakeup` 的新 stream。当前 wakeup prompt 是普通 `role=user`，所以虽然它并非人类输入，投影层仍可能显示为第二条 user 消息。

## 3. 目标状态与接缝边界

```text
模型工具参数
  → Agent 规范化通知策略
  → ProcessSession（启动前一次写入）
  → completion_queue event（携带不可变 origin）
  → WebUI 路由与去重
  → WebUI 运行期 process_id → origin_turn_key 映射
  → process_wakeup context_anchor
  → 模型调用 / durable transcript / WebUI projection / SSE
```

| 状态 | 单一权威 | 写入时机 | 消费方 |
| --- | --- | --- | --- |
| `notify_on_complete` / `watch_patterns` | Agent 的规范化工具参数 | `ProcessSession` 创建前 | reader、poller |
| `origin_ui_session_id` | WebUI 本轮 contextvar | `ProcessSession` 创建前 | WebUI 完成事件路由 |
| `process_id → origin_turn_key` | WebUI 运行期 origin registry | terminal 成功返回后台 `session_id` 时 | completion wakeup、展示投影 |
| 事件投递幂等 | 生产端 UUID `event_id` | 事件首次产生时 | CLI、Gateway、TUI、WebUI drain |
| 最终结果消费 | process-level `_completion_consumed` | wait/log 或 completion 被接受时 | completion 消费者 |
| 内部唤醒语义 | wakeup user 行的结构化 metadata | 建造 wakeup turn 时 | 投影、压缩、turn 对齐 |

`session_key` 仅是兼容回退，不是原始 turn 的来源。缺少精确 owner 或 `origin_turn_key` 映射时必须保守处理并记录诊断，不能挂到“最近一条用户消息”。

## 4. 设计决策

### 4.1 保持显式通知，不设全局 `true` 默认值

把 `notify_on_complete` 改为全局默认 `true` 会让 server、watcher、daemon 被 kill 或崩溃时意外唤醒模型；用户会看到无关后续回答，且无法从调用处判断通知是否会发生。

因此默认契约保持不变：`background=false`、`notify_on_complete=false`、`watch_patterns=None`。模型发起有确定终点的构建、测试、部署或批任务时，应显式传 `notify_on_complete=true`。

若后续需要降低模型漏传率，另开一项受控策略：以可选 `background_notification_policy=explicit|bounded` 提供 profile 级试验。`bounded` 必须是 opt-in，并带命令分类误判监控；它不属于本次可靠性修复的默认路径。

### 4.2 一个后台进程只能选择一种唤醒契约

`notify_on_complete=true` 与 `watch_patterns` 同时传入时，保留完成通知、丢弃 watch。现有互斥规则继续保留，避免中途匹配和最终退出产生双重唤醒。

`watch_patterns` 只适用于长驻进程的稀有里程碑，例如服务 ready。命中频繁时的 15 秒限流、三次连续抑制后关闭 watch 并提升为一次完成通知，维持现有 Agent 行为。

### 4.3 唤醒输入是模型上下文，不是人类输入

所有由 completion queue 驱动的 wakeup prompt 必须使用下列语义，不能仅依赖 `_source` 或英文正文识别：

```json
{
  "role": "user",
  "content": "[IMPORTANT: Background process ...]",
  "_hermes_message_class": "context_anchor",
  "_hermes_scaffold_kind": "process_wakeup",
  "_source": "process_wakeup",
  "_wakeup_meta": {
    "event_id": "6f0ca18be8f34eac9ca2c6dbad15382d",
    "process_id": "proc_xxx",
    "event_type": "completion",
    "origin_ui_session_id": "session_xxx",
    "origin_turn_key": "turn:32"
  }
}
```

这条 anchor 可沿用既有 session turn 的普通保存规则；这只是会话记录本身的既有行为，不构成 completion event 的可靠投递或重启补偿。`drop_non_display_messages()` 必须将其隐藏。它不能启动一个新的可见 user turn。

入队前必须先对 `command` 和 `output` 做与 `process(action=...)` 相同的脱敏。只在 WebUI 日志层脱敏不够，因为 prompt 本身会进入模型上下文和 durable transcript。

## 5. Hermes Agent 改动

Agent 是后台进程的生产者，负责在进程启动前冻结通知策略和 origin 元数据。WebUI 不应事后修改 `ProcessSession` 来弥补竞态。

### 5.1 前置规范化通知参数

**文件：** `hermes-agent/tools/terminal_tool.py`

将 `_resolve_notification_flag_conflict()`、异步回灌能力判断、UI owner 和 Gateway 路由字段的读取移动到 `spawn_local()` / `spawn_via_env()` 之前。所有结果只解析一次，形成不可变的启动快照。

```python
notification = resolve_process_notification_config(
    background=background,
    requested_notify=bool(notify_on_complete),
    requested_watch=watch_patterns,
    async_delivery_supported=async_delivery_supported(),
    session_env=get_session_env,
)

proc_session = process_registry.spawn_local(
    ...,
    notify_on_complete=notification.notify_on_complete,
    watch_patterns=notification.watch_patterns,
    origin_ui_session_id=notification.origin_ui_session_id,
    notification_route=notification.route,
)
```

同一快照必须同时驱动 `ProcessSession`、tool result 与 Gateway watcher registration。不得在 spawn 后再次读取 contextvar 或重新计算冲突规则，否则极快任务和普通任务可能得到不同配置。

`result_data["notify_on_complete"]` 与 `watch_patterns` 必须返回最终生效值。若通道不支持异步回灌，任务仍可在后台启动，但两个通知字段均关闭，并返回 `notify_unsupported` 与主动 poll/wait 指引。

静默任务提示也必须依据最终生效值生成。调用方虽然请求了 notify，但能力检查将其关闭时，不能因为“原始参数为 true”而省略取结果指引。

能力或路由上下文读取异常时应 fail closed：关闭自动通知、保留后台进程启动，并在 tool result 中说明需要 poll/wait。不得把“无法确认是否支持”当成支持。

### 5.2 扩展 spawn 接缝，消除快退出竞态

**文件：** `hermes-agent/tools/process_registry.py`

为 `spawn_local()` 和 `spawn_via_env()` 增加带默认值的 keyword-only 参数。已有调用保持兼容，通知字段在构造 `ProcessSession` 时一次性写入：

```python
notify_on_complete: bool = False,
watch_patterns: Optional[list[str]] = None,
origin_ui_session_id: str = "",
notification_route: Optional[dict[str, str]] = None,
```

`ProcessSession` 增加 `origin_ui_session_id`，并把 route 拷贝到既有 `watcher_platform/chat_id/thread_id/...` 字段。`origin_ui_session_id` 和初始通知配置在 spawn 后不可由其它工具调用覆盖。

运行期状态仍可按既有规则演化：watch 被限流关闭时可以清空/禁用，连续抑制升级时可以把 `notify_on_complete` 设为 true。这里冻结的是启动配置和 owner，不是禁止状态机变化。

仅前置字段不足以消除竞态。三个启动分支都必须遵循：

```text
构造带最终配置的 ProcessSession
→ 启动子进程或远端 wrapper，取得 pid/handle
→ 在 registry._running 中注册 session
→ 启动 reader / PTY reader / env poller
→ 在 registry lock 外写 checkpoint
```

进程即使在 reader 启动前已经退出也没有问题；reader 看到 EOF 后调用 `_move_to_finished()` 时，session 已在 `_running`，因此 `was_running=true`。

注册后若 reader/poller 创建或 `start()` 失败，必须执行统一回滚：从 `_running` 移除 session、终止已启动的进程/进程组、设置 `failed_start`，并同步返回启动失败。不得留下假 running session，也不得发送正常 completion。

PTY、pipe 和非本地环境必须分别测试。`_write_checkpoint()` 内部会再次取得 registry lock，禁止在持有 `_lock` 时调用，避免引入死锁。

删除 `terminal_tool()` 在 spawn 成功后补写 `proc_session.notify_on_complete` 与 `proc_session.watch_patterns` 的代码。这样 reader/poller 第一次观察到进程退出时已拥有最终策略。

### 5.3 completion event 保留 origin 并具备独立身份

**文件：** `hermes-agent/tools/process_registry.py`

本方案不为 terminal 完成事件增加重启补投。`origin_ui_session_id` 只在当前 `ProcessSession` 生命周期内作为精确 owner；Agent 重启后的未投递 terminal event 允许丢失。

所有 terminal 事件通过一个生产函数创建，并在创建时生成 UUID `event_id`。事件重新入队或延迟投递时必须保留该 ID，不能重新生成。

```python
def new_process_event(session, event_type, **payload):
    return {
        "event_id": uuid.uuid4().hex,
        "type": event_type,
        "session_id": session.id,
        "session_key": session.session_key,
        "origin_ui_session_id": session.origin_ui_session_id,
        **payload,
    }
```

`completion`、`watch_match`、`watch_disabled` 和全局 watch 熔断/恢复事件都使用独立 ID。Gateway watcher 合成的 completion 也走同一函数，不能形成第二套事件格式。

Agent 生产的 `event_id` 是业务幂等键；WebUI SSE journal 的帧 ID 是传输游标。WebUI payload 应原样携带 producer event ID，不能在每次 re-emit 时换一个 UUID；SSE 帧仍使用自身 journal ID。

### 5.4 分离事件投递和 completion 消费状态

**文件：** `hermes-agent/tools/process_registry.py`、Agent CLI/Gateway/TUI 消费者、WebUI drain

现有 `_completion_consumed` 以 `process_id` 为键，表示最终输出已经通过 wait/log 或已接受的 completion 路径处理。它不能改成 event ID，也不能由 watch 事件写入。

另增运行期事件 claim 状态，并提供公共方法，替代 WebUI 直接修改 `_completion_consumed` 私有集合：

```python
claim_event_delivery(event_id) -> bool
release_event_delivery(event_id) -> None
mark_completion_consumed(process_id) -> None
```

每个消费者先按 owner 过滤，再原子 claim `event_id`。投递失败时 release；成功启动、进入 deferred 队列或被模型通过当前轮接受后保留 claim。只有 `type="completion"` 才能同时标记 process-level completion consumed。

事件因 owner 不匹配或会话忙而仍需放回共享 `completion_queue` 时，不得长期持有 claim：应在 requeue 前 release，或先判断可接收再 claim。只有事件已从共享队列转移到该会话私有 deferred list 后，才保留 claim。

claim 记录至少保存 `event_id`、`process_id`、`claimed_at`。进程从 registry prune 时清理关联记录；长驻进程的 watch 事件另按有限 TTL 清理，避免当前 WebUI 长时间运行时集合无限增长。

`watch_match`、`watch_disabled` 与熔断事件只消费自己的 event ID，绝不能抑制同一进程稍后的 completion。`poll()` 的 `_poll_observed` 语义保持不变，wait/log 对最终 completion 的抑制行为也保持不变。

### 5.5 在生产边界生成脱敏事件副本

**文件：** `hermes-agent/tools/process_registry.py`

事件入队前复用 `_redact_process_result()` 的规则，但只处理事件副本，不改写 `session.command` 或 `session.output_buffer`。否则 poll/log/wait 的既有输出和调试能力会被意外改变。

处理顺序固定为：清理 ANSI、对完整输出执行 `redact_terminal_output(raw, raw_command)`、对命令执行 `redact_sensitive_text(command, code_file=True)`，最后按通知上限截取尾部。先截断再脱敏可能把敏感值切成无法识别的半段。

该 helper 同时供 `_move_to_finished()`、watch 事件和 Gateway 合成 completion 使用，避免不同入口的脱敏与截断规则继续分叉。

### 5.6 使用 contextvar 传递精确 UI owner

**文件：** `hermes-agent/gateway/session_context.py`、`hermes-agent/tools/terminal_tool.py`

复用现有 `HERMES_UI_SESSION_ID` contextvar 与 `get_session_env()` 映射，不新增 correlation id 或 `turn_key` 相关字段。`origin_turn_key` 是 WebUI 在 terminal 工具结果返回后建立的展示归属，不传给 Agent 的 `ProcessSession`。

`terminal_tool()` 在 spawn 前从 `get_session_env()` 读取 `HERMES_UI_SESSION_ID` 并写入 `ProcessSession`。CLI、cron 和旧 Agent host 没有该值时传空字符串，维持原有 session-key 路由。

该字段是附加的精确 owner。消费者有值时必须优先使用并做正向匹配；没有值时保留旧 `session_key` 兼容路径。不得因字段缺失把 CLI/cron 事件投到任意 WebUI 会话。

### 5.7 Agent 回归测试

**目录：** `hermes-agent/tests/tools/`

| 用例 | 断言 |
| --- | --- |
| 瞬时成功进程 | `background=true, notify_on_complete=true` 恰好入队一次 completion event |
| 瞬时失败进程 | 非零退出也恰好入队一次，保留 exit code 和输出尾部 |
| pipe/PTY/env 注册时序 | reader/poller 观察退出前，session 已存在于 `_running` |
| reader/poller 启动失败 | 进程被终止、registry 无假 running、同步返回 `failed_start` |
| 无通知后台进程 | 未显式 notify/watch 时不入 completion queue |
| watch 命中 | 每次可投递 event 均有不同的 `event_id`，但共享同一 process/origin 字段 |
| watch 后正常退出 | watch event 已投递仍不影响最终 completion 投递 |
| 事件重入队 | 同一事件在 defer/requeue 后保持原 `event_id` |
| 同时传 notify 与 watch | 只产生完成通知，tool result 标出 watch 被忽略 |
| 不支持异步回灌 | 进程仍启动；effective notify/watch 关闭，tool result 给出 poll/wait 指引 |
| 脱敏输出 | 敏感值不进入 queue/prompt；原始 `output_buffer` 不被事件脱敏改写 |
| 并发两会话 | 两个进程 event 的 `origin_ui_session_id` 不串线 |
| 旧调用兼容 | 未传新增 keyword 的 CLI、cron 和测试调用保持原行为 |

## 6. Hermes WebUI 改动

WebUI 是 completion event 的路由者和展示消费者。它不能从 prompt 文本推断内部性，也不能用消息数组尾部推断归属。

### 6.1 保持本轮精确 UI owner

**文件：** `api/streaming.py`

复用已有 `_set_turn_session_identity(session_id)`，使 `HERMES_UI_SESSION_ID` 在当前 agent worker 的 contextvar 内可见。不得新增 Agent 侧 correlation id 或 `turn_key` 字段；`origin_turn_key` 映射只属于 WebUI 运行期 registry。

`ContextVar.reset(token)` 仍是必须的清理方式，防止复用的工作线程把 A 会话 owner 泄露给 B 会话。进程退出事件仅凭 `origin_ui_session_id` 路由到正确会话。

### 6.2 terminal 派发成功时记录原始 turn

**文件：** `api/streaming.py`、新增 `integration/background_process_turns/state.py`

在工具完成 callback 中，仅当 `terminal` 返回成功后台任务（`background=true` 且有非空
`session_id`）时，使用当前稳定 `_manifest_turn_key` 记录：

```python
record_background_process_dispatch(
    webui_session_id=session_id,
    process_id=tool_result["session_id"],
    origin_turn_key=_manifest_turn_key,
    auto_wakeup=bool(tool_result.get("notify_on_complete") or tool_result.get("watch_patterns")),
)
```

registry 的键为 `(webui_session_id, process_id)`，记录至少含 `turn_key`、`status`、
`auto_wakeup` 与创建时间；只保存在当前 WebUI 进程内存，不写 `Session` 模型、SQLite 或 Agent
transcript。进程 id 必须在展示字段中使用 `process:<id>` 前缀，避免与 `delegation_id` 混淆。

记录在 completion settled 后仍须保留，以便原 user 在本次 WebUI 运行期间持续显示关联标记；会话
删除时立即移除，另由受限 TTL 清理已 settled 的旧记录。TTL 清理只移除展示关联，不得影响已写入的
真实 transcript 或重启已被明确排除的 completion 投递。

无论是否自动唤醒，成功启动的后台进程都可以给发起 user 的展示副本附加
`_background_task_ids: ["process:proc_…"]`。未启用 notify/watch 的进程只显示“已启动”关联，
不会自动生成完成唤醒。工具报错、前台 terminal 或缺少稳定 turn key 时不得创建映射。

### 6.3 completion 路由使用精确 owner，旧 Agent 才回退

**文件：** `api/background_process.py`

`_process_one()` 的优先级固定为：

```text
event.origin_ui_session_id
  > ProcessSession.origin_ui_session_id
  > PROCESS_SESSION_INDEX[event.session_key]
  > 无法确认：不投递，记录可诊断失败/重试
```

WebUI 不得在“精确 owner 与 session_key 映射冲突”时选择后者。应记录安全日志，使用精确 owner；该规则已适用于部分现代事件，本次要覆盖 terminal completion、watch match 与 watch disabled。

对不支持新字段的旧 Agent 保留 `session_key` 回退，并把该路径记录为 legacy telemetry。旧路径仅保证会话级归属；只有查到 WebUI registry 的精确 `(session_id, process_id)` 条目时，才允许关联原始 `turn_key`。

### 6.4 用结构化参数创建关联的 wakeup turn

**文件：** `api/background_process.py`、`api/routes.py`

复用现有 `start_session_turn(..., user_message_metadata=..., server_turn_metadata=...)` 接缝；不要新增第二个 `internal_context_anchor` API。浏览器 HTTP 路由仍不得直接提交这些 metadata。

`_start_server_side_wakeup_turn()` 先按精确 session/process 查 registry。查到有效 origin 后，通过
`turn_key_override=origin_turn_key` 与第 4.3 节 metadata 启动 `source="process_wakeup"`；查不到时不得猜测最近 user，也不得伪造 key。`source` 保留用于运行诊断，但不再是唯一语义依据。

`api/routes.py` 对 `turn_key_override` 的白名单应从仅
`async_delegation_wakeup` 扩展为内部的 `async_delegation_wakeup` 与 `process_wakeup`。浏览器
HTTP 路由、Gateway runner 或其它 server-side source 一律不能提供 override；不支持 override 的
runtime adapter 必须明确返回不支持，不能悄悄改为新的 key。

会话已有活跃 stream 时，`record_deferred_wakeup()` 只在内存中保存 `event_id`、process id、已脱敏 prompt、event type、owner 与已解析的 `origin_turn_key`。idle hook 领取到的仍是同一个 structured record，不能退化成裸字符串或再次反查最近 user。

内存 deferred record 在 WebUI 重启后丢失是预期行为。不得新增 SQLite sidecar、ack 或 retry；日志只需记录该限制，不得伪造补投。

### 6.5 保持时间线尾部，用 key 表示展示关联

**文件：** `api/streaming.py`、`integration/agent_message_semantics/`

持久化与投影保持真实发生顺序，后台完成后的 assistant 回答位于时间线尾部。WebUI 在结果合并时为该 wakeup 产生的 assistant/tool 展示副本写入 `_turn_key=origin_turn_key`、`_source="process_wakeup"` 与 `process_id`；原 user 展示副本写入 `process:<id>`。

这些字段只表示归属，绝不改变消息数组顺序、时间戳、SSE 顺序或把迟到结果移动到旧 user 下。若用户已在中间提交 `turn:33`，`turn:32` 的进程结果仍在 `turn:33` 后追加，只是显示层可识别它属于 `turn:32`。

### 6.6 展示投影和 SSE

**文件：** `integration/agent_message_semantics/classifier.py`、`integration/agent_message_semantics/projection.py`、`api/background_process.py`

分类器新增或确认 `process_wakeup` 为 `context_anchor` 的稳定 kind。投影层过滤 anchor，但保留真实 terminal tool call、tool result、后台完成 SSE 状态和最终 assistant 文本；投影层从运行期 registry 将 `process:<id>` 附给原 user。

SSE 的 `bg_task_complete` 继续是纯实时进度事件；它不是普通 `user` SSE。唤醒产生的 assistant token 写入时间线尾部，`done` 只结算这次 wakeup stream。

## 7. 持久化与展示示例

运行时与 durable transcript 可以包含内部 anchor；这里沿用正常会话记录，不新增 completion event 的持久化。WebUI 只隐藏该 anchor，不删除真实工具调用、工具结果和 assistant 正文。示例中 completion assistant 的 `_turn_key` 由 WebUI 结果合并时写入；它不是 Agent `ProcessSession` 的字段，也不用于改变 Agent 执行。

**Agent 运行时 `messages`：** 工具启动记录与内部 wakeup anchor 都会暂留在本次模型调用的上下文中，后者让模型知道后台进程已结束。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:32","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"6f0ca18be8f34eac9ca2c6dbad15382d","process_id":"proc_4a7c","event_type":"completion","origin_turn_key":"turn:32"}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

**Agent durable transcript：** 正常会话保存会保留真实 user、工具调用、工具结果、两段真实 assistant 正文与带结构化语义的 anchor；这不等于为 completion queue 增加了重启恢复能力。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:32","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"6f0ca18be8f34eac9ca2c6dbad15382d","process_id":"proc_4a7c","event_type":"completion","origin_turn_key":"turn:32"}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

**WebUI 展示投影：** 过滤 `process_wakeup` anchor；保留真实的 terminal 调用与工具结果。原 user 取得 `process:proc_4a7c` 标记，完成 assistant 使用同一 `turn:32`；前端可将工具调用和结果渲染为同一张工具卡片，但投影数据不能删除它们。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32","_background_task_ids":["process:proc_4a7c"]},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

### 7.1 后台进程运行时，用户又发送一条真实消息

以下是切片 C 完成后的目标行为。先前的测试仍在运行时，用户可以正常发出第二条请求；它是新的真实 user turn。若测试恰好在该轮 assistant 流式回答期间结束，completion event 先留在运行期内存 deferred 队列，不能插入正在执行的 stream。

**Agent 运行时 `messages`：** 第二条用户输入取得新的 `turn:33`。它结束后，WebUI 用运行期映射查回 `turn:32` 并启动 server-side wakeup；隐藏的 `process_wakeup` anchor 与完成 assistant 都带 `turn:32`，但仍追加在 `turn:33` 的消息之后。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:32","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"6f0ca18be8f34eac9ca2c6dbad15382d","process_id":"proc_4a7c","event_type":"completion","origin_turn_key":"turn:32"}},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

**Agent durable transcript：** 真实用户 turn 仍只有 `turn:32` 与 `turn:33`。完成 anchor 虽带 `turn:32`，但结构化标记表明它是 Agent 上下文，不参与真实用户意图、用户轮次统计或 WebUI user 气泡展示；它和完成 assistant 的 key 只表达与发起任务的关联。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:32","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"6f0ca18be8f34eac9ca2c6dbad15382d","process_id":"proc_4a7c","event_type":"completion","origin_turn_key":"turn:32"}},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

**WebUI 展示投影：** 只隐藏 `turn:32` 的 completion anchor；后台结论仍在第二次真实回答之后按时间线追加。原 user 标记后台进程，完成 assistant 也标记 `turn:32`；页面表现为两段相邻 assistant 回答，前者回答用户的第二个问题，后者是异步测试结论，两者不能错误合并为同一条模型回答。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32","_background_task_ids":["process:proc_4a7c"]},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。","_turn_key":"turn:32","_source":"process_wakeup","process_id":"proc_4a7c"}
]
```

此示例只保证当前运行期内的工具完成、隐藏内部 anchor 与 `process_id → origin_turn_key` 展示关联；未增加跨重启恢复，也不改变跨 turn 的真实时间线顺序。

## 8. 实施顺序与可回滚切片

### 切片 A：Agent 通知配置快照

先引入纯 `resolve_process_notification_config()`，让 tool result、`ProcessSession` 与 Gateway watcher 使用同一份 effective 配置。新增参数全部带默认值，尚不调整 reader/poller 时序。

### 切片 B：Agent 注册时序与失败回滚

分别调整 pipe、PTY、env 三条路径为“注册后启动 reader/poller”，并补齐线程启动失败的终止、移除与同步报错。该切片单独修复瞬时进程漏 completion，不混入 WebUI 行为。

### 切片 C：Agent 事件身份、脱敏与消费契约

统一事件工厂和生产端脱敏，增加 event claim/release 公共 API。逐个迁移 CLI、Gateway、TUI 和 WebUI drain；保留 process-level `_completion_consumed`，且只允许最终 completion 写入。

### 切片 D：WebUI origin registry 与结构化 deferred record

在 terminal 工具成功回调中记录 `process_id → origin_turn_key`，并将原 user 的 `process:<id>`
附加为投影元数据。WebUI 使用公共 event claim，不再直接写 Agent 私有集合；延迟 wakeup 保存
完整内存记录和已解析 origin key。重启后清空 registry 与 pending state 是预期行为。

### 切片 E：隐藏 process wakeup anchor

复用 `start_session_turn` metadata 与受控 `turn_key_override` 接缝，覆盖 eager save、流式合并、
恢复和重放。投影隐藏 anchor，不隐藏真实 assistant 结果；完成 assistant/tool 使用 origin key，
但不改变它们在数组和 SSE 中的尾部位置。

### 切片 F：可选 bounded 通知策略

仅在前五个切片稳定后，才评估 profile 级 `bounded` 策略。默认 `explicit` 永远保留，可一键回退。

建议的 WebUI feature flag：

```text
HERMES_WEBUI_PROCESS_WAKEUP_ANCHORS=false  # 初始默认关闭
```

该 flag 只切换新 event 的 WebUI 投递模式，不改变 Agent 是否投递事件。回退时仍无条件识别并隐藏已存在的 durable anchor，绝不把历史内部提示重新显示成 user。

## 9. WebUI 验收测试

| 场景 | 可观察断言 |
| --- | --- |
| 空闲会话完成 | 原 user 得到 `process:<id>` 投影标记；一次 `bg_task_complete`、一次 wakeup stream、无 user 气泡，结果位于时间线尾部且带原 `_turn_key` |
| 活跃会话期间完成 | 事件进入内存 deferred list；当前 stream 结束后恰好领取一次，不插入 tool/assistant 的非法中间位置，仍使用 dispatch 时记录的 key |
| 极快完成 | Agent event 一定存在，WebUI 不因 event 到达早而漏唤醒 |
| 两会话并发 | A 的 process 不会唤醒 B；冲突时精确 origin 胜出 |
| 两个同会话后台任务 | 各 event ID 只投递一次；同一进程的多次 watch match 不互相吞掉 |
| 中途有新的真实 user | `turn:33` 正常完成后，`turn:32` 的后台结果追加在尾部、带 `turn:32`，绝不新造 `turn:34` 或改写 `turn:33` |
| WebUI 重启 | 不补投旧 completion；不创建错误会话、不伪造 user、不无限重试 |
| Agent 重启 / 旧 Agent | 不补投已完成 event；旧 Agent 新字段缺失时安全降级 session-key |
| watch 限流升级 | watch disabled 与最终 completion 使用不同 event id，各自按契约投递一次 |
| wait/log 已消费 | 只抑制该进程最终 completion，不影响此前或后续独立 watch event |
| 投递失败重试 | release event claim；再次投递沿用原 producer event ID，不生成新 ID |
| 用户删除会话 | completion 找不到 owner 时不创建替代会话、不泄露内容、不无限重试 |
| 压缩与回放 | anchor 留在模型上下文、投影隐藏；结果保持真实时间顺序 |

建议新增或扩展：

```text
tests/test_xsession_wakeup_misroute.py
tests/test_wakeup_defer_race.py
integration/tests/background_process_turns/test_state.py
integration/tests/agent_message_semantics/test_projection.py
```

Agent 测试先在未修复版本上复现“进程立即退出却没有 completion event”，确认测试确实咬住竞态；WebUI 测试需要验证 durable messages、SSE 和最终 projection 三层，而非只断言函数被调用。

还需运行 Agent 的 CLI、Gateway、TUI 通知邻接测试。`completion_queue` 是进程级共享队列，只有 WebUI 用例通过不能证明其它消费者仍保持兼容。

## 10. 运行观测与失败处理

新增结构化日志字段：`event_id`、`process_id`、`event_type`、`origin_ui_session_id`、`origin_turn_key_present`、`route_kind`、`dedup_result`、`delivery_state`。日志只记录已脱敏的命令摘要和截断输出，不记录用户正文。

关键计数器：`process_event_enqueued`、`process_event_claimed`、`process_event_released`、`completion_consumed`、`completion_routed_exact`、`completion_routed_legacy`、`completion_deferred`、`completion_unroutable`、`wakeup_anchor_hidden`。

`completion_unroutable` 不是成功，必须在诊断日志中可见。监控还需按 event type 拆分，避免 watch 流量掩盖 completion 丢失。

事件的失败策略是：先验证 owner，再 claim。路由不确定时 fail closed；投递失败时 release；会话正在执行时写入内存 deferred list 并保留 claim；重复 event 幂等忽略；会话已删除时丢弃且不创建新会话。

缺少 origin mapping 时不得猜测最近 user。可按旧兼容路径处理 completion，但不能使用 `turn_key_override`。watch 事件成功投递后不得调用 `mark_completion_consumed(process_id)`。

## 11. 文件与责任清单

| 仓库 | 文件 | 改动责任 |
| --- | --- | --- |
| Hermes Agent | `tools/terminal_tool.py` | 前置规范化、传入 effective 配置与精确 UI owner |
| Hermes Agent | `tools/process_registry.py` | 注册时序/回滚、事件工厂、脱敏、event claim、completion consumed 公共 API |
| Hermes Agent | `gateway/session_context.py` | 向 terminal 工具暴露已有 UI owner |
| Hermes Agent | `cli.py`、`gateway/run.py`、`tui_gateway/server.py` | 迁移共享队列消费者，区分 event claim 与最终 completion 消费 |
| Hermes WebUI | `api/streaming.py` | 保持本轮 UI owner contextvar 的绑定与清理 |
| Hermes WebUI | `api/background_process.py` | 精确路由、内存去重/defer、structured anchor、server-side wakeup |
| Hermes WebUI | `integration/background_process_turns/state.py` | 运行期 `process_id → origin_turn_key` registry、TTL 与会话删除清理 |
| Hermes WebUI | `api/routes.py` | 复用现有 metadata 参数，限制其只能由内部调用链提供 |
| Hermes WebUI | `integration/agent_message_semantics/` | 分类、投影、历史兼容测试 |
| Hermes WebUI | `docs/architecture/HermesAgent消息补充说明文档.md` | 同步 3.5 的实现状态、持久化与展示示例 |

## 12. 上线判定

切片 A 只证明 effective 配置一致；切片 B 合入后，`notify_on_complete=true` 的瞬时任务不再因注册顺序漏 event。切片 C 合入后，敏感输出不进入事件，watch/completion 不会相互吞掉，四类消费者仍兼容。

切片 D 合入后，后台进程能精确关联发起 user 的 key；切片 E 合入后，用户看不到内部 `[IMPORTANT: ...]` user 气泡，完成结果带回原 key，但不重排时间线。

本方案只能宣称“当前运行期内后台完成通知可靠、结果可关联原始 turn，且内部提示不展示”。它不承诺服务重启后的补投，也不把迟到结果物理移动到原始 turn 的历史位置。
