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
4. 后续真实 assistant 结果保持时间线尾部顺序；进程完成不会伪造一轮人类提问，也不会把迟到结果插回旧位置。
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
  → process_wakeup context_anchor
  → 模型调用 / durable transcript / WebUI projection / SSE
```

| 状态 | 单一权威 | 写入时机 | 消费方 |
| --- | --- | --- | --- |
| `notify_on_complete` / `watch_patterns` | Agent 的规范化工具参数 | `ProcessSession` 创建前 | reader、poller |
| `origin_ui_session_id` | WebUI 本轮 contextvar | `ProcessSession` 创建前 | WebUI 完成事件路由 |
| completion 去重 | 事件级 `event_id` | 事件产生时 | Agent、当前 WebUI drain |
| 内部唤醒语义 | wakeup user 行的结构化 metadata | 建造 wakeup turn 时 | 投影、压缩、turn 对齐 |

`session_key` 仅是兼容回退，不是原始 turn 的来源。缺少精确 owner 时必须保守丢弃 event 并记录诊断，不能挂到“最近一条用户消息”。

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
    "event_id": "proc_xxx:completion:1",
    "process_id": "proc_xxx",
    "event_type": "completion",
    "origin_ui_session_id": "session_xxx"
  }
}
```

这条 anchor 可沿用既有 session turn 的普通保存规则；这只是会话记录本身的既有行为，不构成 completion event 的可靠投递或重启补偿。`drop_non_display_messages()` 必须将其隐藏。它不能启动一个新的可见 user turn。

入队前必须先对 `command` 和 `output` 做与 `process(action=...)` 相同的脱敏。只在 WebUI 日志层脱敏不够，因为 prompt 本身会进入模型上下文和 durable transcript。

## 5. Hermes Agent 改动

Agent 是后台进程的生产者，负责在进程启动前冻结通知策略和 origin 元数据。WebUI 不应事后修改 `ProcessSession` 来弥补竞态。

### 5.1 前置规范化通知参数

**文件：** `hermes-agent/tools/terminal_tool.py`

将 `_resolve_notification_flag_conflict()`、异步回灌能力判断以及 watch/notify 的最终值移动到 `spawn_local()` / `spawn_via_env()` 之前。它们只能计算一次，并使用同一组 `effective_*` 值写入 session 和 tool result。

```python
effective_watch, conflict_note = _resolve_notification_flag_conflict(
    background=background,
    notify_on_complete=bool(notify_on_complete),
    watch_patterns=watch_patterns,
)
effective_notify = bool(background and notify_on_complete and async_delivery_ok)

proc_session = process_registry.spawn_local(
    ...,
    notify_on_complete=effective_notify,
    watch_patterns=effective_watch,
    origin_ui_session_id=origin_ui_session_id,
)
```

`result_data["notify_on_complete"]` 必须写 `effective_notify`，而不是调用方原始值。若当前会话不支持异步回灌，工具结果应继续明确返回 `notify_unsupported`，不允许表现为“已开启但永远不会通知”。

### 5.2 扩展 spawn 接缝，消除快退出竞态

**文件：** `hermes-agent/tools/process_registry.py`

为 `spawn_local()` 和 `spawn_via_env()` 增加以下关键字参数，并在构造 `ProcessSession` 时一次性写入：

```python
notify_on_complete: bool = False,
watch_patterns: Optional[list[str]] = None,
origin_ui_session_id: str = "",
event_sequence: int = 0,
```

`ProcessSession` 增加 `origin_ui_session_id` 与 `event_sequence` 字段。字段应只读语义：spawn 后不得被另一个线程或后续工具调用改写。

删除 `terminal_tool()` 在 spawn 成功后补写 `proc_session.notify_on_complete` 与 `proc_session.watch_patterns` 的代码。这样 reader/poller 第一次观察到进程退出时已拥有最终策略。

### 5.3 completion event 保留 origin 并具备独立身份

**文件：** `hermes-agent/tools/process_registry.py`

本方案不为完成事件做 checkpoint 恢复。`origin_ui_session_id` 与 `event_sequence` 仅在当前 `ProcessSession` 生命周期内有效；Agent 重启后的已完成 event 允许丢失。

`_move_to_finished()`、`_check_watch_patterns()` 和 `watch_disabled` 事件都必须携带同一组 origin 字段，并在生产端生成事件级 `event_id`。completion、watch match、watch disabled 三种事件不能共用 process id 作为去重键。

```python
{
    "type": "completion",
    "session_id": session.id,
    "session_key": session.session_key,
    "event_id": f"{session.id}:completion:{session.event_sequence}",
    "origin_ui_session_id": session.origin_ui_session_id,
    ...
}
```

`event_sequence` 在 session lock 下递增；它只服务于当前进程内的 event-id 生成，不要求 checkpoint 保存。WebUI 对 watch event 只做 event-id 去重；Agent 的 `_completion_consumed` 只标记最终 completion，绝不能因为一次 watch match 吞掉以后 completion。

在把 event 放入 `completion_queue` 之前，复用 `redact_terminal_output` 与 `redact_sensitive_text` 清理 command/output。相同 helper 也应由 `process(action=...)` 使用，避免两条输出面逐渐分叉。

### 5.4 使用 contextvar 传递精确 UI owner

**文件：** `hermes-agent/gateway/session_context.py`、`hermes-agent/tools/terminal_tool.py`

复用现有 `HERMES_UI_SESSION_ID` contextvar 与 `get_session_env()` 映射，不新增 correlation id 或 `turn_key` 相关字段。

`terminal_tool()` 从 `get_session_env()` 读取 `HERMES_UI_SESSION_ID` 并写入 `ProcessSession`。CLI、cron 和旧 Agent host 没有该值时传空字符串，维持原有行为。

### 5.5 Agent 回归测试

**目录：** `hermes-agent/tests/tools/`

| 用例 | 断言 |
| --- | --- |
| 瞬时成功进程 | `background=true, notify_on_complete=true` 恰好入队一次 completion event |
| 瞬时失败进程 | 非零退出也恰好入队一次，保留 exit code 和输出尾部 |
| 无通知后台进程 | 未显式 notify/watch 时不入 completion queue |
| watch 命中 | 每次可投递 event 均有不同的 `event_id`，但共享同一 process/origin 字段 |
| 同时传 notify 与 watch | 只产生完成通知，tool result 标出 watch 被忽略 |
| 脱敏输出 | API key、密码和 Authorization 值不会进入 queue、模型 prompt 或 durable transcript |
| 并发两会话 | 两个进程 event 的 `origin_ui_session_id` 不串线 |

## 6. Hermes WebUI 改动

WebUI 是 completion event 的路由者和展示消费者。它不能从 prompt 文本推断内部性，也不能用消息数组尾部推断归属。

### 6.1 保持本轮精确 UI owner

**文件：** `api/streaming.py`

复用已有 `_set_turn_session_identity(session_id)`，使 `HERMES_UI_SESSION_ID` 在当前 agent worker 的 contextvar 内可见。不得新增 correlation id、origin sidecar 或 `turn_key` 映射。

`ContextVar.reset(token)` 仍是必须的清理方式，防止复用的工作线程把 A 会话 owner 泄露给 B 会话。进程退出事件仅凭 `origin_ui_session_id` 路由到正确会话。

### 6.2 completion 路由使用精确 owner，旧 Agent 才回退

**文件：** `api/background_process.py`

`_process_one()` 的优先级固定为：

```text
event.origin_ui_session_id
  > ProcessSession.origin_ui_session_id
  > PROCESS_SESSION_INDEX[event.session_key]
  > 无法确认：不投递，记录可诊断失败/重试
```

WebUI 不得在“精确 owner 与 session_key 映射冲突”时选择后者。应记录安全日志，使用精确 owner；该规则已适用于部分现代事件，本次要覆盖 terminal completion、watch match 与 watch disabled。

对不支持新字段的旧 Agent 保留 `session_key` 回退，并把该路径记录为 legacy telemetry。旧路径仅保证会话级归属；本方案不做原始 `turn_key` 关联。

### 6.3 用结构化参数创建 wakeup turn

**文件：** `api/background_process.py`、`api/routes.py`

复用现有 `start_session_turn(..., user_message_metadata=..., server_turn_metadata=...)` 接缝；不要新增第二个 `internal_context_anchor` API。浏览器 HTTP 路由仍不得直接提交这些 metadata。

`_start_server_side_wakeup_turn()` 通过上述 metadata 传入第 4.3 节结构，而不是把 `wakeup_prompt` 当成普通用户文本。`source="process_wakeup"` 保留用于运行诊断，但不再是唯一语义依据。

会话已有活跃 stream 时，`record_deferred_wakeup()` 只在内存中保存 `event_id`、process id、已脱敏 prompt、event type 与 owner。idle hook 领取到的仍是同一个 structured record，不能退化成裸字符串。

内存 deferred record 在 WebUI 重启后丢失是预期行为。不得新增 SQLite sidecar、ack 或 retry；日志只需记录该限制，不得伪造补投。

### 6.4 保持时间线尾部，不回挂旧 turn

**文件：** `api/streaming.py`、`integration/agent_message_semantics/`

持久化与投影保持真实发生顺序，后台完成后的 assistant 回答位于时间线尾部。本方案不写 `origin_turn_key`，不改 manifest 分组，也不把迟到结果移动到旧 user 下。

### 6.5 展示投影和 SSE

**文件：** `integration/agent_message_semantics/classifier.py`、`integration/agent_message_semantics/projection.py`、`api/background_process.py`

分类器新增或确认 `process_wakeup` 为 `context_anchor` 的稳定 kind。投影层过滤 anchor，但保留真实 terminal tool call、tool result、后台完成 SSE 状态和最终 assistant 文本。

SSE 的 `bg_task_complete` 继续是纯实时进度事件；它不是普通 `user` SSE。唤醒产生的 assistant token 写入时间线尾部，`done` 只结算这次 wakeup stream。

## 7. 持久化与展示示例

运行时与 durable transcript 可以包含内部 anchor；这里沿用正常会话记录，不新增 completion event 的持久化。WebUI 只隐藏该 anchor，不删除真实工具调用、工具结果和 assistant 正文。

**Agent 运行时 `messages`：** 工具启动记录与内部 wakeup anchor 都会暂留在本次模型调用的上下文中，后者让模型知道后台进程已结束。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"proc_4a7c:completion:1","process_id":"proc_4a7c","event_type":"completion"}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

**Agent durable transcript：** 正常会话保存会保留真实 user、工具调用、工具结果、两段真实 assistant 正文与带结构化语义的 anchor；这不等于为 completion queue 增加了重启恢复能力。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"proc_4a7c:completion:1","process_id":"proc_4a7c","event_type":"completion"}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

**WebUI 展示投影：** 过滤 `process_wakeup` anchor；保留真实的 terminal 调用与工具结果。前端可将这两条渲染为同一张工具卡片，但投影数据不能删除它们。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

### 7.1 后台进程运行时，用户又发送一条真实消息

以下是切片 C 完成后的目标行为。先前的测试仍在运行时，用户可以正常发出第二条请求；它是新的真实 user turn。若测试恰好在该轮 assistant 流式回答期间结束，completion event 先留在运行期内存 deferred 队列，不能插入正在执行的 stream。

**Agent 运行时 `messages`：** 第二条用户输入取得新的 `turn:33`。它结束后，WebUI 才启动 `turn:34` 的 server-side wakeup；`turn:34` 的 user 行是隐藏的 `process_wakeup` anchor，而非第三条人类输入。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:34","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"proc_4a7c:completion:1","process_id":"proc_4a7c","event_type":"completion"}},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。"}
]
```

**Agent durable transcript：** 三个 turn 都按发生顺序保存。`turn:34` 虽有 `role="user"`，但结构化标记表明它是 Agent 上下文，不参与真实用户意图、用户轮次统计或 WebUI user 气泡展示。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=1).]","_turn_key":"turn:34","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"process_wakeup","_source":"process_wakeup","_wakeup_meta":{"event_id":"proc_4a7c:completion:1","process_id":"proc_4a7c","event_type":"completion"}},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。"}
]
```

**WebUI 展示投影：** 只隐藏 `turn:34` 的 anchor；后台结论在第二次真实回答之后按时间线追加。页面表现为两段相邻 assistant 回答：前者回答用户的第二个问题，后者是异步测试结论；两者不能错误合并为同一条模型回答。

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"output\":\"Background process started\",\"session_id\":\"proc_4a7c\",\"notify_on_complete\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"在测试期间，顺便说明登录超时最可能影响哪些场景。","_turn_key":"turn:33"},
  {"role":"assistant","content":"登录超时会主要影响弱网登录、令牌刷新和高峰期并发登录；测试完成后我会结合失败用例判断。"},
  {"role":"assistant","content":"测试现已完成：失败用例是 test_refresh_token_timeout，和令牌刷新阶段的登录超时一致。"}
]
```

此示例只保证当前运行期内的工具完成和隐藏内部 anchor；未增加跨重启或跨 turn 的额外关联字段。

## 8. 实施顺序与可回滚切片

### 切片 A：Agent 初始化、脱敏与事件身份

先改 Agent 的通知参数前置、`ProcessSession` 构造、生产侧脱敏和事件级 `event_id`。WebUI 暂按旧 session-key 兼容消费。该切片修复“显式启用却漏通知”和敏感输出外泄。

### 切片 B：WebUI 内存去重与结构化 deferred record

将 WebUI 去重从 process id 改为 event id；延迟 wakeup 保存完整内存记录并按 event id 去重。重启后清空 pending state 是预期行为，记录诊断即可。

### 切片 C：隐藏 process wakeup anchor

复用 `start_session_turn` metadata 接缝，覆盖 eager save、流式合并、恢复和重放。投影隐藏 anchor，不隐藏真实 assistant 结果。

### 切片 D：可选 bounded 通知策略

仅在前三个切片稳定后，才评估 profile 级 `bounded` 策略。默认 `explicit` 永远保留，可一键回退。

建议的 WebUI feature flag：

```text
HERMES_WEBUI_PROCESS_WAKEUP_ANCHORS=false  # 初始默认关闭
```

该 flag 只切换新 event 的 WebUI 投递模式，不改变 Agent 是否投递事件。回退时仍无条件识别并隐藏已存在的 durable anchor，绝不把历史内部提示重新显示成 user。

## 9. WebUI 验收测试

| 场景 | 可观察断言 |
| --- | --- |
| 空闲会话完成 | 一次 `bg_task_complete`、一次 wakeup stream、无 user 气泡、结果位于时间线尾部 |
| 活跃会话期间完成 | 事件进入内存 deferred list；当前 stream 结束后恰好领取一次，不插入 tool/assistant 的非法中间位置 |
| 极快完成 | Agent event 一定存在，WebUI 不因 event 到达早而漏唤醒 |
| 两会话并发 | A 的 process 不会唤醒 B；冲突时精确 origin 胜出 |
| 两个同会话后台任务 | 各 event id 只投递一次；同一进程的多次 watch match 不互相吞掉 |
| WebUI 重启 | 不补投旧 completion；不创建错误会话、不伪造 user、不无限重试 |
| Agent 重启 / 旧 Agent | 不补投已完成 event；旧 Agent 新字段缺失时安全降级 session-key |
| watch 限流升级 | watch disabled 与最终 completion 使用不同 event id，各自按契约投递一次 |
| 用户删除会话 | completion 找不到 owner 时不创建替代会话、不泄露内容、不无限重试 |
| 压缩与回放 | anchor 留在模型上下文、投影隐藏；结果保持真实时间顺序 |

建议新增或扩展：

```text
tests/test_xsession_wakeup_misroute.py
tests/test_wakeup_defer_race.py
integration/tests/agent_message_semantics/test_projection.py
```

Agent 测试先在未修复版本上复现“进程立即退出却没有 completion event”，确认测试确实咬住竞态；WebUI 测试需要验证 durable messages、SSE 和最终 projection 三层，而非只断言函数被调用。

## 10. 运行观测与失败处理

新增结构化日志字段：`event_id`、`process_id`、`event_type`、`origin_ui_session_id`、`route_kind`、`dedup_result`、`delivery_state`。日志只记录已脱敏的命令摘要和截断输出。

关键计数器：`completion_enqueued`、`completion_routed_exact`、`completion_routed_legacy`、`completion_deferred`、`completion_deduped`、`completion_unroutable`、`wakeup_anchor_hidden`。`completion_unroutable` 不是成功，必须在诊断界面或日志中可见。

完成 event 的失败策略是：路由不确定时 fail closed；会话正在执行时写入内存 deferred list；重复 event 幂等忽略；会话已删除则丢弃事件且不创建新会话。不得为“保证显示”而把消息投到最近活动会话。

## 11. 文件与责任清单

| 仓库 | 文件 | 改动责任 |
| --- | --- | --- |
| Hermes Agent | `tools/terminal_tool.py` | 前置规范化、传入 effective 参数与精确 UI owner |
| Hermes Agent | `tools/process_registry.py` | session 初始化、脱敏、事件级 id、三类 event metadata |
| Hermes Agent | `gateway/session_context.py` | 向 terminal 工具暴露已有 UI owner |
| Hermes WebUI | `api/streaming.py` | 保持本轮 UI owner contextvar 的绑定与清理 |
| Hermes WebUI | `api/background_process.py` | 精确路由、内存去重/defer、structured anchor、server-side wakeup |
| Hermes WebUI | `api/routes.py` | 复用现有 metadata 参数，限制其只能由内部调用链提供 |
| Hermes WebUI | `integration/agent_message_semantics/` | 分类、投影、历史兼容测试 |
| Hermes WebUI | `docs/architecture/HermesAgent消息补充说明文档.md` | 同步 3.5 的实现状态、持久化与展示示例 |

## 12. 上线判定

切片 A 合入后，`notify_on_complete=true` 的瞬时任务不再漏 event，且敏感输出不会进入 completion event。切片 B 合入后，同一运行期内 watch/completion 不会相互吞掉；切片 C 合入后，用户看不到内部 `[IMPORTANT: ...]` user 气泡。

本方案只能宣称“当前运行期内后台完成通知可靠且内部提示不展示”。它不承诺服务重启后的补投，也不承诺后台结果回挂原始 turn。
