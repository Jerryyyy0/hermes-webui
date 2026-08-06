# 异步委派完成的 Turn 对齐与实时展示

- **状态：** Implemented
- **作者：** @wzq
- **创建日期：** 2026-08-06
- **关联契约：** [WebUI Run State Consistency Contract](webui-run-state-consistency-contract.md)、[Session Inspector Manifest](../session-inspector-manifest.md)

## 问题

Hermes Agent 的后台委派完成后，WebUI 目前将完成摘要作为普通
`role=user` 文本传给 `start_session_turn(..., source="process_wakeup")`。例如：

```text
[ASYNC DELEGATION BATCH COMPLETE - deleg_267a7280]
```

这段文本对 Agent 是有意义的模型上下文，但它不是用户的新请求。把它当作普通
user 消息会产生三个错误：

1. WebUI transcript、`GET /api/session` 和普通 SSE 出现一条用户从未输入的消息。
2. `_message_turns` 将它视为新的 user anchor，产生虚假的 `turn:N`。
3. 完成后的 assistant、工具、MEDIA 和 artifact 结算可能绑定到这个假 turn，或在
   用户已经发出后续真实请求时错误绑定到最新 user turn。

问题不应通过修改 Agent 注入的原始 `role` 或 `content` 解决。该文本仍需进入 Agent
上下文，以自动开始完成后的后续处理。

## 目标

- 保留 Agent 内部注入消息的原始 `role=user` 与原始正文，使其继续参与模型上下文。
- 内部 completion 不显示在 transcript、`GET /api/session`、普通 SSE、分页、回放、导出
  或 Manifest turn 中。
- 异步完成后仍自动继续 Agent，并将后续 token 实时送达浏览器。
- 每个 session 同时最多运行一条 WebUI agent stream；异步结果不得与普通新 turn 并发流式。
- 后续 assistant/tool/MEDIA/artifact 的**逻辑归属**保持为派发后台任务的真实 user turn。
- 异步完成的 assistant 输出按到达时刻追加到可见时间线尾部，不回插到历史位置。
- 在原始 turn 上维护一个非 transcript 的后台任务生命周期标记。
- 不迁移、重绑或按正文猜测处理历史会话。

## 非目标

- 不修改异步委派任务自身的 `async_delegations` 数据库表、CLI、Gateway、TUI 或
  `delegate_tool` 协议；Agent 的 canonical `messages` 持久化表需要增加幂等元数据，以避免
  上下文重放被误写成新的普通 user。
- 不向 Hermes Agent 的异步委派完成事件加入 `origin_turn_key`。
- 不改变 completion 注入的原文，也不以占位文本替换它。
- 不让历史无语义标记的普通 user 消息因为正文相似而被隐藏。
- 不进行 `rebind_manifest_turn` 或旧 artifact store 迁移。

## 术语与不变量

| 名称 | 定义 |
| --- | --- |
| 真实 user turn | 用户提交并带 `_turn_key` 的消息，是 Manifest 的唯一 turn anchor。 |
| 委派源 turn / origin turn | 成功派发某个 `delegation_id` 的真实 user turn。 |
| completion anchor | Agent 为异步完成自动注入的 `role=user` 消息；语义类别为 `context_anchor`，不是新 user turn。 |
| 显示顺序 | 可见消息按实际到达时间追加的顺序。 |
| 逻辑归属 | assistant、工具、MEDIA、artifact 与 Manifest 使用的 `_turn_key`。 |
| 后台任务标记 | 挂在 origin turn 元数据上的非 transcript 生命周期状态。 |

必须同时成立：

```text
stream_turn_key == origin_turn_key == 最新的真实 user._turn_key（对该委派而言）
```

这里的“最新”是指委派创建当时对应的真实 user turn，不是在 completion 到达时向后扫描
会话得到的全局最新 user。

## 用户可见行为

用户可以在后台任务运行时继续对话。异步任务完成后，结果不回插到旧位置，而在当前
时间线末尾实时流出；这保留了时间顺序。它可能紧接另一条 assistant 气泡，这一视觉结果
是允许且诚实的，不能为避免两条连续 assistant 而制造假 user 消息。

示例：

```text
19:00  user       turn:8  派发后台任务
19:00  assistant  turn:8  已派发后台任务
19:01  user       turn:9  新问题
19:02  assistant  turn:9  新问题回复
19:05  assistant  turn:8  后台任务的流式回复
```

浏览器显示的 transcript 为：

```text
user:      派发后台任务
assistant: 已派发后台任务
user:      新问题
assistant: 新问题回复
assistant: 后台任务的流式回复
```

而模型上下文包含未显示的 completion anchor：

```text
user:      派发后台任务                 _turn_key=turn:8
assistant: 已派发后台任务                _turn_key=turn:8
user:      新问题                       _turn_key=turn:9
assistant: 新问题回复                   _turn_key=turn:9
user:      [原始异步完成正文]            context_anchor / async_delegation_completion
assistant: 后台任务的流式回复             _turn_key=turn:8
```

completion anchor 不显示、不产生普通 SSE `message` 事件，也不成为 turn anchor。最后一条
assistant、其工具事件、`MEDIA:`、References 和 artifact 都属于 `turn:8`。

### 单 session 单流规则

如果 completion 到达时 `turn:9` 仍在流式处理，后台结果不会立即启动第二条 stream，也不
会插入或改写 `turn:9` 的 assistant 内容。WebUI 先把唤醒请求放入该 session 的有序
`async_delegation_wakeup` 队列：

```text
19:02  turn:9  assistant 正在流式回复
19:03  deleg_123 completion 到达，记录为 queued，不启动第二条 stream
19:04  turn:9  assistant 结算
19:04  turn:8  开始独立的后台完成回复并实时流式输出
```

这样“实时”表示当前 stream 释放后立即开始 token 流，不表示同一 session 内的两个 Agent
run 并行。队列按 completion 到达顺序处理；每个队列项仍携带自己的 `delegation_id`、
`origin_turn_key` 和 hidden completion anchor。若当前 stream 失败、取消或连接断开，队列
也必须按既有生命周期规则明确继续、取消或标记失败，不能静默丢弃。

## 设计

### 1. Agent 最小语义桥

Agent 已有消息语义字段：

```python
_hermes_message_class
_hermes_scaffold_kind
```

为 `AIAgent.run_conversation`、`agent.conversation_loop.run_conversation` 和
`agent.turn_context.build_turn_context` 增加可选参数：

```python
user_message_metadata: Mapping[str, str] | None = None
```

仅允许透传：

```text
_hermes_message_class
_hermes_scaffold_kind
```

异步完成唤醒时，Agent 构造：

```python
{
    "role": "user",
    "content": completion_text,  # 原始完成正文，完全不改写
    "_hermes_message_class": "context_anchor",
    "_hermes_scaffold_kind": "async_delegation_completion",
}
```

该桥只表达“这条输入是隐藏的模型上下文”，不传递 WebUI 的 turn 所有权。Provider API
副本继续删除 `_hermes_*` 与旧兼容 flag，外部模型不会收到内部元数据。

### 2. WebUI 记录 origin，而非推断

WebUI 在一个成功的后台 `delegate_task` 结果到达时，以当前不可变的
`stream_turn_key` 写入 session sidecar：

```json
{
  "async_delegation_origins": {
    "deleg_123": {
      "turn_key": "turn:8",
      "created_at": 1786004400,
      "status": "running",
      "wakeup_state": "idle"
    }
  }
}
```

`delegation_id` 是该映射的唯一键。WebUI 不得从 completion 文本、消息尾部或“最新 user”
推断来源。这个 mapping 是 WebUI 自己的显示/归属状态，因此不扩大 Hermes Agent 的
`async_delegation` 表或事件协议。

同一 origin turn 可有多个 delegation id；它们必须是独立的 sidecar 记录和独立的
生命周期条目。sidecar 查找必须以完整 session/profile 身份隔离。

### 3. 原始 turn 的后台任务状态

sidecar 在成功派发后记录并通过 SSE 发送一个非 transcript 状态：

```yaml
turn:8:
  background_tasks:
    - delegation_id: deleg_123
      status: running
      dispatched_at: "19:00"
      completed_at: null
```

状态只能为 `running`、`completed`、`failed` 或 `cancelled`。`wakeup_state` 只能为 `idle`、
`queued`、`running`、`settled` 或 `failed`，用于区分委派任务本身和完成后的 Agent 唤醒。
例如 completion 已到达但 `turn:9` 仍活动时，任务 `status=completed`、
`wakeup_state=queued`。该标记属于 turn 活动/Inspector 投影，而不是 `messages[]` 的 user
或 assistant 行。刷新、回放、重连时从 sidecar 恢复，不得通过 transcript 反推。

### 4. Completion 唤醒与 SSE

`api/background_process.py` 接到 completion 后执行：

1. 从 completion 中获得 `delegation_id`。
2. 由 sidecar 解析 origin record 与 `turn_key`，将任务状态更新为 `completed`。
3. 检查 session 是否已有活动 stream：
   - 空闲：立即进入第 5 步启动唤醒；
   - 活动：写入有序 wakeup 队列，设置 `wakeup_state=queued`，等待当前 stream 的终态事件。
4. 当前 stream 释放后，以 `source="async_delegation_wakeup"`、
   `turn_key_override=origin_turn_key` 和上述 `user_message_metadata` 调用内部
   `start_session_turn`，设置 `wakeup_state=running`。因此 `server_turn_started` 只在实际
   获得 stream 所有权时发送，而不是 completion 到达时强行开第二条流。payload 扩展为：

```json
{
  "source": "async_delegation_wakeup",
  "delegation_id": "deleg_123",
  "origin_turn_key": "turn:8"
}
```

5. 浏览器按既有 live stream 接入该 run，实时渲染 assistant token、工具与状态；可见输出
   追加在当前时间线尾部。
6. run 结束后将 `wakeup_state` 更新为 `settled` 或 `failed`，并通过状态 SSE 发送更新。

不得新建平行 transport，也不得把 completion anchor 作为普通 `message` SSE 广播。

### 5. 前端如何接收后台消息

前端保持一个会话级 SSE 监听通道。该通道负责传递后台任务状态和 server-side turn 的
启动通知；具体 assistant token 仍通过 `stream_id` 对应的既有 chat stream 接收。

当普通 `turn:9` 仍在运行时，completion 到达会先发送状态事件：

```json
{
  "event": "bg_task_complete",
  "data": {
    "delegation_id": "deleg_123",
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "queued"
  }
}
```

前端只更新 `turn:8` 的后台任务状态，不创建隐藏 completion 的用户行，也不尝试
`POST /api/chat/start`。当前 `turn:9` 收到 `done`、`stream_end`、`error` 或 `cancel` 等终态后，
后端 session scheduler 消费 wakeup 队列并启动后台 Agent run，然后发送：

```json
{
  "event": "server_turn_started",
  "data": {
    "session_id": "session_123",
    "stream_id": "stream_async_123",
    "source": "async_delegation_wakeup",
    "delegation_id": "deleg_123",
    "origin_turn_key": "turn:8"
  }
}
```

前端收到 `server_turn_started` 后复用现有 `attachLiveStream(stream_id)` 连接，接收后台
assistant token、tool、MEDIA 和终态事件，并把可见内容追加到当前时间线尾部。前端不再为
该 wakeup 发送第二次 `/api/chat/start`；这样既不会重复启动，也不会产生并发 stream。

若浏览器未连接，后端仍可完成 server-side wakeup；浏览器重新打开或 SSE 重连时，通过
已持久化的 session/run 状态恢复 `server_turn_started` 或相应的 stream replay。若用户在
后台 wakeup stream 活跃期间提交新消息，前端保留输入并等待现有 stream 终态；后端按单
session 调度规则接受或排队该输入，绝不并行执行。

### 6. mapping 缺失或竞态

派发工具 callback 与 completion 可能竞态。completion 到达但 origin mapping 尚未落盘时，
`api/background_process.py` 仅在有上限的短暂重试窗口内重新读取 sidecar。

重试耗尽后必须 fail closed：

- 不自动 continuation；
- 不新建 turn；
- 不回退绑定最新真实 user；
- 仍发送已有的 completion 状态/确认信息；
- 记录 `async_delegation_origin_unresolved`，包含 session、delegation id 和可安全记录的
  原始 completion 内容。

这样宁可需要人工恢复，也不会把后台结果写到错误用户问题下。

### 7. 统一语义显示投影

WebUI 的 `integration/agent_message_semantics/` 是分类和显示投影的唯一入口。
分类优先级为：

1. `_hermes_message_class`；
2. 旧 synthetic flag；
3. 无标记即普通消息。

不根据正文或 `api_content` 猜测。`context_anchor` 与 `internal_scaffold` 都从 display
projection 删除，但保留在 Agent model context 所需的位置。

显示过滤在以下所有路径的分页、`msg_limit`、`msg_before` 和 `turn_align` 之前执行：

- `GET /api/session` 的 sidecar/state.db 合并结果；
- SSE 普通 assistant/user 消息投影；
- streaming merge 的 `previous_display`、`previous_context`、`result_messages`；
- replay/recovery、session export/share 等 transcript 输出。

`_merge_display_messages_after_agent_result` 在三份列表复制后、prefix/delta 计算前调用
`drop_non_display_messages()`，候选循环再作一次防御性跳过。还必须阻止“当前输入缺失于显示
transcript 时自动补一个可见 user”的 fallback 把 completion anchor 重新实体化。

### 8. Turn、Manifest 与 artifact

`_latest_user_turn_binding`、`_message_turns`、pending checkpoint/recovered pending turn 查找、
`_next_turn_key` 和 artifact persistence 前的 user-anchor 校验，都必须将
`internal_scaffold` 和 `context_anchor` 视为非真实 user。

对于异步 wakeup，`turn_key_override` 是其后续 run 的权威归属：

```text
真实 user turn:8
completion anchor (hidden)
assistant/tool/MEDIA/artifact from async wakeup
             └── 全部写为/结算为 turn:8
```

Manifest 仍是派生索引，不是 transcript 或执行 journal。它可以显示 `turn:8` 的 background
task lifecycle，但不会以 completion anchor 新建 `manifest.turns[]` 项；普通 `turns[]` 仍只
来自真实 user anchor。

### 9. Agent 写入幂等与数据库约束

异步 wakeup 会重新构造一份包含原始 user 的模型上下文。该 user 不是新的用户输入，但如果
复制后的消息字典没有 `_DB_PERSISTED_MARKER`，`run_agent.py::_flush_messages_to_session_db_unlocked`
会把它误判为新消息。因此内存 marker 只能作为快速路径，不能作为持久化幂等的唯一依据。

Agent 的 canonical `messages` 表增加一个可空字段：

```sql
hermes_dedupe_key TEXT
```

并创建只约束新写入、活动记录的部分唯一索引：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_hermes_dedupe
ON messages(session_id, hermes_dedupe_key)
WHERE active = 1 AND hermes_dedupe_key IS NOT NULL;
```

幂等键由消息来源生成并在 replay/recovery/wakeup 复制时保留：

```text
真实 user：       user:<origin_turn_key>
异步 completion：  context:<delegation_id>:completion
internal scaffold：不持久化，不生成幂等键
```

`_turn_key` 必须在 WebUI 创建真实 turn 时作为内部持久化元数据传入 Agent；它和
`hermes_dedupe_key` 都不是 Provider API 字段。相同正文但不同真实 turn 必须生成不同幂等键，
因此不能使用 `session_id + role + content` 做唯一约束。

`SessionDB.append_message`、批量插入、replay/recovery 和消息重写路径统一使用事务级幂等写入：

```sql
INSERT INTO messages (...)
VALUES (...)
ON CONFLICT DO NOTHING;
```

这里使用无目标的 `ON CONFLICT DO NOTHING`，因为唯一性由带 `active` 条件的部分索引提供；
实现必须通过插入结果判断是否真的新增了行，不能仅依赖 `lastrowid`。

只有实际插入成功时才更新 `sessions.message_count` 与 `tool_call_count`；冲突时返回已有
行 ID，并记录 `persistence_skip`。internal scaffold 仍直接跳过；context anchor 保留原始
正文和语义字段，但不成为普通 user turn。

迁移策略保持保守：为旧数据库增加可空列和索引，不回填历史行，不删除已有重复数据，不执行
turn rebind 或 artifact store 迁移。旧行的幂等键为空，因此不会阻塞索引创建；新写入消息才使用
该约束。

## 日志

所有消息语义和异步归属决策使用结构化前缀：

```text
hermes_message_semantics
```

至少覆盖：

```text
action=created|persistence_skip|persistence_write|display_drop|
       get_projection_drop|turn_binding_skip|manifest_turn_skip|
       async_delegation_origin_recorded|async_delegation_origin_resolved|
       async_delegation_origin_unresolved|background_task_status
class=...
kind=...
role=...
content='完整原文'
session_id=...
turn_key=...
delegation_id=...
```

`content` 使用 `%r` 输出原始完整正文，不以占位词替换；日志接入既有敏感信息脱敏策略，不应
绕过全局日志安全边界。

## 实现范围

| 层 | 变更 |
| --- | --- |
| Hermes Agent | 增加 `messages.hermes_dedupe_key` 可空列和活动行部分唯一索引；统一 append、批量、replay/recovery 写入幂等；保留 `user_message_metadata` 最小透传桥，为 completion anchor 赋予 `context_anchor/async_delegation_completion` 语义。 |
| WebUI sidecar | 保存 `delegation_id -> origin_turn_key` 与 per-turn 后台任务状态。 |
| `api/background_process.py` | 解析 origin，调用带 `turn_key_override` 的 wakeup；处理有界竞态与失败关闭。 |
| `api/streaming.py` | 使用语义显示投影、避免 completion fallback visible user、沿用 override 结算所有输出。 |
| `api/session_manifest.py` | 非真实 user 不切 turn；origin turn 收集异步 run 的工具/MEDIA/artifact。 |
| 前端/SSE | 扩展现有 `server_turn_started` 和任务状态事件；在时间线尾部实时展示该 run，不渲染 hidden anchor。 |

## 测试与验收

### 自动化覆盖

| 场景 | 可观察断言 |
| --- | --- |
| Agent completion metadata | 原始 `role`/`content` 不变；`context_anchor` 和 kind 正确；Provider 副本没有内部字段。 |
| 派发记录 | `delegate_task` 成功后 sidecar 保存对应 `delegation_id` 和创建时 `stream_turn_key`。 |
| completion 无后续对话 | completion 不显示；最终 assistant 与 artifact 归属 origin turn。 |
| completion 前有新 user turn | completion 不绑定新 turn；当前 turn 结束后才启动 origin 的 assistant 流，`_turn_key` 仍是 origin。 |
| completion 到达时当前 turn 仍活动 | 不创建并发 stream、不混入当前 assistant；sidecar 为 `wakeup_state=queued`，当前 turn 终态后按顺序启动。 |
| 前端事件交互 | `bg_task_complete` 只更新任务状态；`server_turn_started` 携带 `stream_id` 后前端复用现有 live-stream 连接，不重复 POST wakeup。 |
| 多个后台任务 | 每个 delegation id 独立解析、更新状态，不能交叉结算。 |
| 多 profile/session | origin mapping 不能跨 profile 或 session 命中。 |
| callback/completion 竞态 | mapping 在重试窗口内落盘可继续；窗口耗尽不 continuation、不新建 turn。 |
| 语义投影 | `GET /api/session`、分页、SSE、merge、replay、导出均不含 completion anchor；真实最终 assistant 保留。 |
| Manifest/artifact | synthetic user 不生成 turn；`turn:8` 的 MEDIA、工具和 artifact 持久化成功。 |
| Agent 持久化幂等 | async wakeup 重放原始 user 时只保留一条 active 数据库记录；重复 flush 不重复增加 session 计数；不同 turn 的相同正文仍分别保留。 |
| 历史数据 | 旧 flag 仍被识别；无任何语义字段的普通历史 user 即使正文相同也保留。 |

建议新增/更新：

```text
integration/tests/agent_message_semantics/
tests/test_issue5334_verification_stop_leak.py
tests/test_artifact_turn_isolation.py
tests/test_session_manifest.py
tests/test_async_delegation_turn_alignment.py
```

### 手动验收

1. 在 `turn:8` 派发后台任务，确认该 turn 显示 `running` 的非 transcript 任务标记。
2. completion 前发送 `turn:9` 并获得回复。
3. 在 `turn:9` 仍流式时确认收到 `bg_task_complete`，但没有第二条 assistant stream。
4. `turn:9` 收到终态后确认收到 `server_turn_started`，前端使用其 `stream_id` 连接后台流，未重复调用 `/api/chat/start`。
5. 确认 completion 的原始 user 正文未显示；后台 assistant token 在时间线尾部实时出现。
6. 确认这段晚到 assistant 的工具、MEDIA、artifact chips 与 Manifest 都属于 `turn:8`。
7. 刷新、重连、切换会话后确认后台任务标记和已收到的输出不重复、不转移归属。
8. 模拟 mapping 缺失，确认日志为 `async_delegation_origin_unresolved`，且没有假 user 或错误新 turn。

## 发布与兼容

部署顺序：

1. 先部署 Agent metadata bridge，使新 completion 可被明确分类。
2. 再部署 WebUI sidecar origin mapping、显示过滤、turn override 和 SSE 投影。
3. 先执行可重复的 `messages` 表加列和部分唯一索引迁移；迁移失败时 fail closed，不启动
   新的 async wakeup 写入。
4. 保留旧 synthetic flag 的回退识别，兼容新旧版本短暂并存。

已存在的会话不做幂等键回填、turn rebind、store 重写或正文清理。新版本仅对带稳定幂等键的
新写入执行数据库约束；没有标记的历史消息保持原样，并由 WebUI 读取投影负责兼容隐藏/合并。
