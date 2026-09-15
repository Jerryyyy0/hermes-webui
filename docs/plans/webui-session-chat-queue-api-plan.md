# WebUI 会话消息 Queue 后端接口方案

**状态：** Proposed（仅设计，不代表已实现）

**范围：** 同一 WebUI 会话内的用户消息排队、排序、编辑、删除，以及将待处理消息转换为当前活跃 Agent run 的补充指令。

**关联契约：** [`../rfcs/webui-pending-intent-controls.md`](../rfcs/webui-pending-intent-controls.md)、[`../rfcs/live-to-final-assistant-replies.md`](../rfcs/live-to-final-assistant-replies.md)、[`../rfcs/session-sse-contract-v1.md`](../rfcs/session-sse-contract-v1.md)。

**关联实现：** [`../../integration/pending_chat_turns.py`](../../integration/pending_chat_turns.py)、[`../../api/routes.py`](../../api/routes.py)、[`../../api/streaming.py`](../../api/streaming.py)、[`../../static/ui.js`](../../static/ui.js)。

## 1. 结论

采用“一个已有提交入口、一个查询接口、一个统一命令接口”的极简方案：

```text
POST /api/chat/start          提交用户消息；会话空闲时立即启动，忙碌时持久入队
GET  /api/chat/queue          查询当前会话的权威队列快照
POST /api/chat/queue/commands 排序、开始编辑、提交编辑、取消编辑、删除、转为补充指令
```

如果该能力仅作为当前 Fork 的定制功能落地，应遵守 integration 边界，将两个新增接口实现为：

```text
GET  /api/integration/chat_queue
POST /api/integration/chat_queue/commands
```

两组路径只选择一组，不同时维护别名。本文后续使用较短的 `/api/chat/queue` 表示其逻辑契约；Fork 实现时按上面的 integration 路径映射。

方案的核心原则：

1. 后端会话队列是唯一权威状态；浏览器内存、`sessionStorage` 和 `localStorage` 只能做临时缓存。
2. 同一会话仍然最多只有一个 active Agent stream。“多个任务同时问”表示可以连续提交多个任务，但后端按队列顺序串行答复，不表示同一会话并行运行多个 Agent turn。
3. `/api/chat/start`、队列命令、自动 drain、取消和 Steer 必须共享同一个 session 级控制锁及同一份队列状态。
4. Queue 与 Supplement/Steer 是不同语义：Queue 等当前 run 结束后开始一个正常新 turn；Supplement/Steer 属于当前 active run。
5. “Agent 已接收补充指令”和“模型已经读取补充指令”必须分开建模。没有 Agent 侧证据时，WebUI 只能声明前者。

## 2. 当前基础与缺口

### 2.1 已有能力

- `POST /api/chat/start` 在 WebUI 会话忙碌时，可以把消息持久化到 `session.pending_next_turns` 并返回 `202 queued`。
- `drain_pending_chat_turn(session_id)` 会在会话空闲后领取第一条 `queued` 项，启动后继 stream。
- 浏览器端 `SESSION_QUEUES` 已支持队列卡片、拖动排序、文本编辑和删除。
- `POST /api/chat/steer` 可以把文本交给当前进程缓存的 Agent，在下一个工具结果边界注入模型上下文。
- `pending_steer_leftover` 可以表示 run 结束时仍未消费的 Steer 文本。

### 2.2 当前缺口

- 浏览器队列和后端 `pending_next_turns` 是两份不同状态，没有统一版本和同步协议。
- 后端没有队列明细查询、排序、编辑、删除接口。
- `pending_next_turns` 当前主要面向 FIFO drain，没有正式的可编辑状态机。
- `/api/chat/steer` 只返回是否被 Agent 接受，没有稳定的 `supplement_id`，也没有模型已应用的回调。
- 当前 Steer 指示主要是临时 DOM 状态，不能单独保证刷新、会话切换或历史回放后仍可重建。
- Steer 在 Agent 模型输入中可能表现为 `tool.content` 内的 out-of-band 标记，不能把该内部结构当成 WebUI 历史展示协议。

## 3. 状态所有权

| 状态 | 权威 owner | 持久位置 | 说明 |
| --- | --- | --- | --- |
| 待执行 Queue | WebUI session | `pending_next_turns` 或等价 session 字段 | 排序、编辑、删除、drain 的唯一数据源 |
| Queue schema/revision | WebUI session | `queue_schema_version`、`queue_revision` | schema version 描述数据结构；revision 在每次队列可见变更后递增 |
| active stream/run | WebUI runtime | 现有 stream、`ACTIVE_RUNS` 和 session generation | 判断能否启动、取消或注入 |
| Supplement 投递状态 | WebUI + Agent runtime | `turn_interventions`/journal + Agent 回调 | 区分 delivering、delivered、applied、leftover |
| 浏览器 Queue UI | 浏览器 | 内存中的服务端快照 | 仅用于渲染和乐观更新，不拥有最终状态 |

第一期可以继续扩展 `session.pending_next_turns`，不新增第二份数据库队列。若未来需要多个 WebUI 进程共同消费，才考虑迁移到带事务、租约和 outbox 的独立持久层。

## 4. Queue 数据模型

本方案区分三种数据形态：最小入队请求、持久化基础 Queue Item，以及只在特定状态下出现的扩展字段。不能把所有可能字段都预先写成 `null`，否则基础结构过重，也难以看出每个字段由哪个状态拥有。

### 4.1 最小入队请求

调用方创建一条普通 Queue Item 时，最少提交：

```json
{
  "session_id": "session-001",
  "message": "只分析登录模块，不要修改代码",
  "idempotency_key": "message-20260915-001"
}
```

约束：

- `session_id`、`message` 必填。
- 新接口要求 `idempotency_key` 必填；兼容旧 `/api/chat/start` 调用时可以暂时允许缺失，但只能保证消息被保留，不能保证 HTTP 重试最多入队一次。
- `attachments`、`model`、`model_provider`、`workspace` 和 `profile` 是可选请求字段。
- 服务端生成 `entry_id`、时间戳、初始状态和版本号，调用方不能指定。

### 4.2 逻辑队列容器

逻辑上的持久结构为：

```json
{
  "version": 2,
  "revision": 7,
  "items": [
    {
      "entry_id": "queue-001",
      "idempotency_key": "message-20260915-001",
      "text": "只分析登录模块，不要修改代码",
      "attachments": [],
      "status": "queued",
      "item_version": 1,
      "created_at": 1789441200
    }
  ]
}
```

为兼容当前 Session 模型，第一期不必立刻把 `pending_next_turns` 从数组迁移成对象。可以采用等价映射：

```text
session.queue_schema_version = 2
session.queue_revision = 7
session.pending_next_turns = [QueueItem, ...]
```

这样既能保留当前 `pending_next_turns` 的读取和恢复路径，又避免在每条 Queue Item 上重复保存 schema version、revision 和 session ID。以后若迁移到独立表或完整容器，HTTP 契约不需要随存储形态一起变化。

### 4.3 最小持久化 Queue Item

Queue 调度本身要求的最小字段是：

| 字段 | 说明 |
| --- | --- |
| `entry_id` | 服务端生成的稳定身份；接口和 UI 不使用数组下标定位项目 |
| `idempotency_key` | 消息提交重试去重键 |
| `text` | 用户消息正文 |
| `attachments` | 已规范化的附件引用；无附件时为空数组 |
| `status` | 当前队列状态 |
| `item_version` | 单条项目的乐观并发版本 |
| `created_at` | 入队时间 |

`session_id` 不在单条记录中重复保存，因为 Queue Item 已由所属 Session 容器隔离。日志、SSE 和 HTTP 响应需要时由容器上下文补充。

`position` 不持久化。数组顺序是权威顺序，GET 响应可按当前数组下标计算 `position`。排序成功后只重排数组并递增 `queue_revision`。

### 4.4 执行环境快照

如果产品选择“任务按入队时配置执行”，Queue Item 增加可选的执行快照：

```json
{
  "execution": {
    "workspace": "/workspace/project",
    "model": "gpt-5",
    "model_provider": "openai",
    "profile": "default"
  }
}
```

推荐在入队时由服务端解析并冻结完整执行快照，避免用户之后切换模型、Provider、Profile 或 workspace，导致已排队任务的执行环境静默变化。兼容旧记录时，如果 `execution` 缺失，dispatch 可以沿用当前 Session 配置，但必须把这种回退视为兼容契约并记录诊断信息。

### 4.5 状态专用字段

以下字段只在对应状态存在，不在基础记录中预置 `null`。

编辑中：

```json
{
  "status": "editing",
  "edit_token": "edit-token-001",
  "edit_started_at": 1789441220,
  "edit_lease_expires_at": 1789441820
}
```

正在 dispatch 或重试失败：

```json
{
  "status": "dispatching",
  "dispatch_token": "dispatch-token-001",
  "attempts": 1
}
```

```json
{
  "status": "failed",
  "attempts": 2,
  "last_error": "chat start failed",
  "updated_at": 1789441300
}
```

来源于 Stop-and-send 或 leftover Steer：

```json
{
  "source": "stop_and_send",
  "origin_stream_id": "stream-000",
  "origin_generation": 3
}
```

转为当前 run 的 Supplement：

```json
{
  "status": "supplement_delivering",
  "supplement_id": "supplement-001",
  "target_stream_id": "stream-000",
  "target_generation": 3
}
```

`target_stream_id + target_generation` 必须共同约束 Supplement 的目标，不能只按 session ID 注入。

## 5. Queue 状态机

```text
queued
 ├─→ editing ───────────────→ queued
 ├─→ dispatching ───────────→ sent
 ├─→ supplement_delivering ─→ supplement_delivered ─→ supplement_applied
 ├─→ deleted
 └─→ failed ────────────────→ queued

supplement_delivering
 ├─→ queued             明确确认尚未注入
 └─→ delivery_unknown   无法确认 Agent 是否已经接收

supplement_delivered
 └─→ queued             run 结束前未应用，作为 leftover 重新排队
```

操作权限：

- `queued`：可以排序、开始编辑、删除、转为 Supplement。
- `editing`：可以提交编辑、取消编辑、删除；不能被 drain。
- `dispatching`：不能排序、编辑或删除；启动失败后恢复为 `queued` 或进入 `failed`。
- `supplement_delivering`：禁止重复操作，直到得到明确结果或进入 `delivery_unknown`。
- `supplement_delivered`：已离开普通 Queue，不能再编辑、删除或作为下一 turn 发送。
- `delivery_unknown`：不能自动重试注入，需要恢复界面提示用户选择保留、重新排队或放弃。
- `sent`、`deleted`、`supplement_applied`：终态，不计入待处理数量。

当队首处于 `editing` 时，推荐阻塞后续 drain，不能跳过它发送第二项，否则用户看到的顺序与实际执行顺序会发生变化。

## 6. 接口一：`POST /api/chat/start`

### 6.1 请求

```http
POST /api/chat/start
Content-Type: application/json
```

```json
{
  "session_id": "session-001",
  "message": "分析登录模块",
  "attachments": [],
  "model": "gpt-5",
  "model_provider": "openai",
  "workspace": "/workspace/project",
  "idempotency_key": "message-20260915-001"
}
```

客户端应为每次逻辑发送生成稳定的 `idempotency_key`。网络重试必须复用相同 key。

### 6.2 会话空闲

后端在 session lock 内确认不存在阻塞启动的 active stream/run，按现有流程启动 Agent：

```http
HTTP/1.1 200 OK
```

```json
{
  "status": "running",
  "session_id": "session-001",
  "stream_id": "stream-001"
}
```

### 6.3 会话忙碌

后端在同一把 session lock 内追加 Queue Item、递增 `queue_revision` 并保存 session：

```http
HTTP/1.1 202 Accepted
```

```json
{
  "status": "queued",
  "queued": true,
  "session_id": "session-001",
  "entry_id": "queue-001",
  "position": 1,
  "queue_revision": 7,
  "active_stream_id": "stream-000",
  "retryable": true
}
```

前端不应先通过状态接口判断 busy 再决定调用哪个提交接口。是否立即启动或入队必须由 `/api/chat/start` 在使用点原子决定，避免 check-then-use 竞态。

### 6.4 重复提交

相同 session 内再次提交同一个 `idempotency_key` 时，返回现有条目或已启动结果，不重复追加 Queue Item：

```json
{
  "status": "queued",
  "queued": true,
  "entry_id": "queue-001",
  "duplicate": true,
  "queue_revision": 7
}
```

## 7. 接口二：`GET /api/chat/queue`

### 7.1 请求

```http
GET /api/chat/queue?session_id=session-001
```

### 7.2 响应

```json
{
  "session_id": "session-001",
  "queue_schema_version": 2,
  "queue_revision": 7,
  "active_run": {
    "stream_id": "stream-000",
    "generation": 3,
    "status": "running"
  },
  "items": [
    {
      "entry_id": "queue-001",
      "position": 0,
      "status": "queued",
      "item_version": 1,
      "text": "分析登录模块",
      "attachments": [],
      "execution": {
        "workspace": "/workspace/project",
        "model": "gpt-5",
        "model_provider": "openai",
        "profile": "default"
      },
      "created_at": 1789441200
    },
    {
      "entry_id": "queue-002",
      "position": 1,
      "status": "editing",
      "item_version": 2,
      "text": "补充移动端测试",
      "attachments": [],
      "execution": {
        "workspace": "/workspace/project",
        "model": "gpt-5",
        "model_provider": "openai",
        "profile": "default"
      },
      "created_at": 1789441260
    }
  ]
}
```

使用场景：

- 页面首次加载或刷新；
- 切换回目标会话；
- SSE 重连或收到 `queue_changed`；
- 命令返回 revision conflict；
- 多标签页需要收敛到同一状态。

默认只返回仍需要用户关注的项目，例如 `queued`、`editing`、`failed` 和 `delivery_unknown`。终态记录保留在内部审计或 timeline 中，不计入 `items` 和角标数量。

## 8. 接口三：`POST /api/chat/queue/commands`

### 8.1 公共请求信封

```http
POST /api/chat/queue/commands
Content-Type: application/json
```

```json
{
  "session_id": "session-001",
  "command_id": "command-001",
  "action": "reorder",
  "expected_queue_revision": 7
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | Queue 所属会话 |
| `command_id` | string | 是 | 一次逻辑命令的幂等标识；重试必须复用 |
| `action` | string | 是 | 命令类型 |
| `expected_queue_revision` | integer | 按 action | 集合级乐观并发控制 |
| `entry_id` | string | 按 action | 稳定 Queue Item ID |
| `expected_item_version` | integer | 按 action | 项目级乐观并发控制 |

公共成功响应：

```json
{
  "accepted": true,
  "action": "reorder",
  "queue_revision": 8,
  "item": null
}
```

公共 revision 冲突响应：

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "queue_revision_conflict",
  "message": "队列已经发生变化，请刷新后重试",
  "queue_revision": 9,
  "items": []
}
```

后端应缓存或持久记录已完成的 `command_id` 及结果摘要，使浏览器因超时重试同一命令时得到相同结果，而不是重复执行。

### 8.2 `reorder`：拖动排序

请求提交当前所有可排序项目的完整顺序：

```json
{
  "session_id": "session-001",
  "command_id": "reorder-001",
  "action": "reorder",
  "expected_queue_revision": 7,
  "ordered_entry_ids": [
    "queue-002",
    "queue-001",
    "queue-003"
  ]
}
```

服务端校验：

1. `ordered_entry_ids` 不得重复。
2. 必须完整覆盖当前全部可排序 `queued` 项。
3. 不得包含 `dispatching`、`supplement_delivering` 或终态项目。
4. `expected_queue_revision` 必须等于当前 revision。
5. 校验和写入都在同一 session lock 内完成。

采用完整 ID 顺序，不采用 `from_index/to_index`。数组下标会因后台 drain 或其它标签页追加任务而失效。

### 8.3 `begin_edit`：回到输入框编辑

```json
{
  "session_id": "session-001",
  "command_id": "begin-edit-001",
  "action": "begin_edit",
  "entry_id": "queue-001",
  "expected_item_version": 1
}
```

后端原子执行：

```text
queued → editing
```

响应：

```json
{
  "accepted": true,
  "action": "begin_edit",
  "queue_revision": 8,
  "edit_token": "edit-token-001",
  "draft": {
    "entry_id": "queue-001",
    "text": "分析登录模块",
    "attachments": [],
    "model": "gpt-5",
    "model_provider": "openai"
  }
}
```

前端把 `draft` 放入输入框并保留 `entry_id + edit_token`。不能使用“前端先读取内容、再删除原队列项”的两步流程，因为 drain 可能在两步之间启动该项目。

### 8.4 `commit_edit`：提交编辑

```json
{
  "session_id": "session-001",
  "command_id": "commit-edit-001",
  "action": "commit_edit",
  "entry_id": "queue-001",
  "edit_token": "edit-token-001",
  "text": "只分析登录模块，不要修改代码",
  "attachments": [],
  "model": "gpt-5",
  "model_provider": "openai"
}
```

后端校验 edit token 和项目状态后执行：

```text
editing → queued
```

编辑后的项目默认回到原相对位置。若产品决定编辑后的任务应移动到队尾，必须作为显式契约记录，不能由数组删除后重新追加偶然决定。

### 8.5 `cancel_edit`：放弃编辑

```json
{
  "session_id": "session-001",
  "command_id": "cancel-edit-001",
  "action": "cancel_edit",
  "entry_id": "queue-001",
  "edit_token": "edit-token-001"
}
```

恢复编辑前保存的内容并执行：

```text
editing → queued
```

如果浏览器关闭或 edit lease 超时，后端也应安全恢复为 `queued`，不能永久卡住队列。恢复策略应保存原内容快照，并通过会话事件通知其它页面。

### 8.6 `delete`：删除排队任务

```json
{
  "session_id": "session-001",
  "command_id": "delete-001",
  "action": "delete",
  "entry_id": "queue-002",
  "expected_item_version": 2
}
```

只允许：

```text
queued  → deleted
editing → deleted
failed  → deleted
```

如果项目已经开始 dispatch 或 Supplement 投递，返回：

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "queue_item_not_mutable",
  "message": "任务已经开始处理，不能从队列中删除",
  "status": "dispatching"
}
```

删除可以从活跃数组中移除，但应保留短期 tombstone 或 timeline 记录用于命令幂等和问题诊断。

### 8.7 `send_as_supplement`：转为当前任务补充信息

```json
{
  "session_id": "session-001",
  "command_id": "supplement-001",
  "action": "send_as_supplement",
  "entry_id": "queue-003",
  "expected_queue_revision": 10,
  "target_stream_id": "stream-000",
  "target_generation": 3,
  "idempotency_key": "supplement-queue-003-stream-000"
}
```

后端流程：

1. 在 session lock 内重新读取 session 和目标 Queue Item。
2. 确认项目仍为 `queued`，目标 `stream_id + generation` 仍是当前 active run。
3. 把项目更新为 `supplement_delivering`，生成稳定 `supplement_id` 并持久化。
4. 释放 session lock，再调用 RuntimeAdapter 或当前 Agent 的 `steer()`。不得在 session lock 内进行跨进程或网络等待。
5. 明确拒绝或确认未送达时，重新加锁并恢复为 `queued`。
6. Agent 接受时，重新加锁更新为 `supplement_delivered`，写入可回放 intervention 记录。
7. Agent 证明下一次模型调用已包含该内容时，更新为 `supplement_applied`。
8. 如果 run 在应用前结束，转成来源为 `leftover_steer` 的普通 `queued` 项，不能丢弃。

接口接受响应：

```http
HTTP/1.1 202 Accepted
```

```json
{
  "accepted": true,
  "action": "send_as_supplement",
  "entry_id": "queue-003",
  "supplement_id": "supplement-001",
  "delivery_state": "delivered",
  "target_stream_id": "stream-000",
  "queue_revision": 11
}
```

目标已经变化时必须失败关闭：

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "active_stream_changed",
  "message": "当前任务已经发生变化，补充信息仍保留在队列中",
  "entry_id": "queue-003",
  "status": "queued"
}
```

## 9. Supplement 的 UI、SSE 与历史语义

### 9.1 不把 accepted 当成 applied

当前 Agent 的 `steer(text)` 只证明文本已被缓存在 Agent 中。文本通常要等到下一个工具结果边界才会进入下一次模型输入。因此：

- `supplement_delivered`：WebUI 可以显示“已收到用户补充指令，继续处理中”。
- `supplement_applied`：只有收到 Agent/TUI Gateway 的消费证据后，才能显示“已读取用户补充指令”。
- 没有 `supplement_applied` 能力时，UI 不得根据等待时间、后续 token 或 tool 输出猜测模型已经读取。

### 9.2 会话事件

复用已有的会话级 SSE：

```http
GET /api/sessions/{session_id}/events
```

建议新增可回放事件：

```text
queue_changed
supplement_delivered
assistant_segment_completed
supplement_applied
supplement_fallback_queued
```

`queue_changed` 只携带失效通知和 revision，前端收到后重新读取权威快照：

```json
{
  "event_id": "event-101",
  "session_id": "session-001",
  "queue_revision": 11,
  "reason": "send_as_supplement"
}
```

`supplement_delivered`：

```json
{
  "event_id": "event-102",
  "session_id": "session-001",
  "stream_id": "stream-000",
  "generation": 3,
  "queue_entry_id": "queue-003",
  "supplement_id": "supplement-001",
  "text": "请同时考虑移动端"
}
```

`supplement_applied`：

```json
{
  "event_id": "event-103",
  "session_id": "session-001",
  "stream_id": "stream-000",
  "supplement_id": "supplement-001",
  "applied_at_model_iteration": 4
}
```

事件必须先持久化、后广播。仅发送临时 SSE 而不写 session/journal，会导致刷新和重连后无法恢复相同时间线。

### 9.3 展示顺序

前端收到 `supplement_delivered` 后：

1. 固化补充前的 assistant 展示段。
2. 显示“已收到用户补充指令，继续处理中”。
3. 插入带 `kind=supplement` 的用户消息气泡。
4. 在同一 `stream_id` 下建立新的 assistant continuation 展示段。

收到 `supplement_applied` 后，可以把边界文案升级为：

```text
上一阶段已完成 · 已读取用户补充指令
```

这里的“已完成”只表示补充前的可视 assistant segment 已结束，不表示整个 Agent run 已经进入 `done`。最终回答仍属于同一个 run 和同一个稳定 assistant-turn owner。

目标展示：

```text
Assistant 原执行内容

✓ 上一阶段已完成 · 已读取用户补充指令

User 补充：请同时考虑移动端

Assistant 继续执行过程……
Assistant 最终回答……
```

### 9.4 历史持久化

不要在 Queue Item 刚创建时就把它写成普通 `Session.messages.role=user`，否则尚未执行的任务可能进入 Agent 历史上下文。

建议分别保存：

```text
pending_next_turns  调度队列
turn_interventions 补充指令、投递状态和 assistant segment 边界
Session.messages    Agent/provider 的正常持久对话上下文
```

历史读取接口把 `turn_interventions` 投影为用户可见事件：

```json
{
  "role": "user",
  "content": "请同时考虑移动端",
  "metadata": {
    "kind": "supplement",
    "queue_entry_id": "queue-003",
    "supplement_id": "supplement-001",
    "stream_id": "stream-000",
    "delivery_state": "applied"
  }
}
```

该投影用于 WebUI 历史展示，不应再次作为普通下一 turn 用户消息重复喂给 Agent。实现时可以把 intervention 投影接入现有 `activity_scene_v1`/稳定 assistant-turn anchor，而不是依赖解析 `tool.content` 内部的 out-of-band 标记。

## 10. 自动 drain

当前 active run 结束、取消或 teardown 后，由后端检查队列：

```text
读取第一个可执行 queued 项
        ↓
锁内 queued → dispatching，写 dispatch_token/attempts 并保存
        ↓
释放锁，调用现有 chat-start 内核
        ↓
启动成功：dispatching → sent
启动失败：dispatching → queued/failed
```

不变量：

1. drain 只能领取 `queued` 项。
2. 一个 Queue Item 同一时间只有一个有效 `dispatch_token`。
3. 启动新 stream 前必须再次验证 session 没有 active stream/run。
4. 只有成功获得后继 `stream_id` 后才能标记 `sent`。
5. worker teardown、cancel settle、服务启动恢复都可以触发 drain，但依靠同一把锁和 lease 保证不会重复启动。
6. `editing` 队首阻塞后续项目；`delivery_unknown` 不自动转换或跳过，需用户处理。

## 11. 并发与幂等

### 11.1 锁范围

所有 Queue 读改写必须使用 `_get_session_agent_lock(session_id)` 或其统一服务封装。

锁内只允许：

- 重新读取 session；
- 校验 active stream/generation；
- 校验 revision/version/token；
- 修改 Queue Item、intervention 和 session 控制字段；
- `session.save()`。

锁内不得等待：

- LLM/provider 请求；
- Agent `steer()` 的跨进程或网络实现；
- SSE 客户端；
- 文件上传；
- worker 退出。

### 11.2 Revision

- `queue_revision`：排序、追加、开始/结束编辑、删除、dispatch claim、Supplement 状态变化时递增。
- `item_version`：单条项目内容或状态变化时递增。
- revision 冲突返回 `409` 和最新 revision；调用方重新 GET 后决定是否重试。

### 11.3 幂等键

- 消息提交：`idempotency_key`。
- Queue 命令：`command_id`。
- Supplement 投递：稳定 `supplement_id` 或由 `entry_id + target stream generation` 派生的幂等键。

“Agent 已接收，但 HTTP 响应丢失”是最危险的 Supplement 竞态。Agent 侧没有投递 ID 去重前，WebUI 不能盲目重试，只能记录 `delivery_unknown`。

### 11.4 多标签页

多个标签页可以同时读取队列，但修改必须携带 revision。一个页面修改成功后广播 `queue_changed`；其它页面重新 GET。旧 revision 的拖动、编辑或删除请求返回冲突，不能覆盖新状态。

### 11.5 多进程与 Gateway

当前本地 `SESSION_AGENT_CACHE` 具有进程亲和性。如果运行多个 WebUI worker，`send_as_supplement` 必须被路由到 active run owner，或通过 RuntimeAdapter/TUI Gateway 控制面发送。无法确认 owner 时应保留 Queue Item 并返回明确失败，不能向任意进程的缓存 Agent 注入。

## 12. 错误约定

| HTTP | error | 场景 |
| --- | --- | --- |
| `400` | `invalid_queue_command` | 请求字段或 action 非法 |
| `404` | `session_not_found` | 会话不存在或调用方不可见 |
| `404` | `queue_item_not_found` | Queue Item 不存在 |
| `409` | `queue_revision_conflict` | 队列 revision 已变化 |
| `409` | `queue_item_version_conflict` | 项目已经被修改 |
| `409` | `queue_item_not_mutable` | 项目已经 dispatch/投递/终结 |
| `409` | `active_stream_changed` | Supplement 目标 run 已变化 |
| `409` | `supplement_delivery_unknown` | 无法确认是否已经注入，禁止自动重试 |
| `422` | `supplement_unsupported` | 当前 backend/Agent 不支持 Steer |
| `500` | `queue_persist_failed` | session 队列持久化失败 |

面向调用方的 `message` 使用中文；内部异常详情只进入服务端日志，不直接暴露。

普通接口继续使用现有 `hermes_session` cookie 认证和 CSRF 约定。服务端必须验证 session 属于当前调用方可见 Profile，不能仅凭请求中的 session ID 修改其它 Profile 的队列。

## 13. 前端接入

### 13.1 数据源迁移

当前 `SESSION_QUEUES` 可以保留为页面内缓存，但内容必须来自 `GET /api/chat/queue`。逐步停止直接把 `sessionStorage/localStorage` 当成队列持久层。

推荐流程：

1. 加载会话时 GET 服务端队列。
2. `POST /api/chat/start` 返回 `202 queued` 时，把响应项目乐观加入当前快照。
3. 收到 `queue_changed` 后重新 GET 收敛。
4. 拖动完成后提交完整 ID 顺序；冲突时回滚并刷新。
5. 编辑按钮调用 `begin_edit`，成功后再回填 Composer。
6. 删除按钮只在服务端成功后移除，或乐观移除但保留可回滚快照。
7. Supplement 按 delivered/applied 两阶段显示，不用单个 toast 代替持久时间线。

### 13.2 控件布局

- Queue 卡片继续位于 Composer 上方。
- 拖动手柄作为主要操作。
- 编辑、删除、转 Supplement 属于单项操作；窄屏可放入单项溢出菜单，避免每行堆满图标。
- `delivery_unknown`、`failed` 必须有可见状态和恢复动作，不能只写控制台日志。

UI 实施时需要按仓库规范补桌面、窄屏和移动端前后对比证据。

## 14. 推荐代码边界

若作为 Fork 能力实现：

```text
integration/chat_queue/
  __init__.py
  models.py       Queue Item 归一化、状态和版本
  service.py      list/command/drain/supplement 的业务原子操作
  handlers.py     GET/POST HTTP 解析与 j()/bad() 响应
  projection.py   Queue 与 turn_interventions 的历史/SSE 投影

integration/tests/chat_queue/
  test_handlers.py
  test_service.py
  test_concurrency.py
  test_supplement.py
```

上游接缝只保留薄调用：

- `api/routes.py`：integration GET/POST 分发和 `/api/chat/start` 入队钩子。
- `api/streaming.py`：worker teardown 后 drain、Supplement leftover/applied 事件钩子。
- `static/ui.js`、`static/messages.js`：调用接口和渲染状态；复杂 Fork UI 逻辑优先放入 `integration/assets/`。

新增 integration HTTP 接口时，同步更新：

- `integration/swagger/openapi.json`
- `integration/README.md`
- `integration/CHANGELOG.md`（功能实际实现并产生用户可见行为时）

## 15. 分阶段实施

### 阶段一：后端 Queue CRUD 与排序

- 增加逻辑 Queue 容器 v2，并以 Session 的 `queue_schema_version + queue_revision + pending_next_turns` 兼容映射落地。
- 为 Queue 集合增加 `queue_revision`，为单条 Queue Item 增加 `item_version`。
- 实现 GET、`reorder`、`begin_edit`、`commit_edit`、`cancel_edit`、`delete`。
- 让 `/api/chat/start` 和 drain 调用同一个 Queue Service。
- 前端读取后端快照，浏览器存储降级为非权威缓存。

该阶段不改变 Steer 语义。

### 阶段二：Supplement delivered 与持久时间线

- 实现 `send_as_supplement`。
- 增加稳定 `supplement_id` 和 `turn_interventions`。
- 发出可回放 `queue_changed`、`supplement_delivered`、`supplement_fallback_queued`。
- 历史接口和 `activity_scene_v1` 能恢复 Supplement 用户消息及前后 segment。
- UI 只显示“已收到用户补充指令”，不声称模型已读取。

### 阶段三：Agent applied 证明

关联 Hermes Agent/TUI Gateway 增加：

- `steer(supplement_id, text)` 或等价带 ID 控制接口；
- 模型输入真正包含该补充内容后的 `supplement_applied` 回调；
- leftover 返回同一 `supplement_id`；
- 重复 ID 去重。

完成后 WebUI 才启用“已读取用户补充指令”文案。

### 阶段四：恢复与多 owner

- 服务启动时恢复 `dispatching` lease、`editing` lease 和 `delivery_unknown`。
- 验证 Gateway 与本地 Agent 两种 backend。
- 明确多 WebUI worker 下的 active run owner 路由。

## 16. 测试矩阵

### 16.1 Queue 基础行为

1. 会话空闲时 `/api/chat/start` 立即启动。
2. 会话忙碌时连续提交多条消息，按提交顺序持久入队。
3. 相同 `idempotency_key` 重试不会重复入队。
4. 页面刷新、会话切换和多标签页读取到同一队列。
5. 零项、一项、多项和重复请求都返回稳定结构。

### 16.2 排序、编辑和删除

1. 完整 ID 排序成功并递增 revision。
2. 排序与 drain 并发时，只有一个操作基于匹配 revision 成功。
3. `begin_edit` 与 drain 并发时，项目只能进入 `editing` 或 `dispatching` 之一。
4. 编辑期间刷新仍能恢复草稿和 edit token/lease。
5. 删除与 dispatch 并发时，不会出现“接口报告删除成功但任务仍启动”。
6. 旧 revision/version 命令返回 `409`，不覆盖新状态。

### 16.3 Supplement

1. 只允许向匹配的 active `stream_id + generation` 投递。
2. Agent 明确拒绝时项目恢复 `queued`。
3. Agent 接受后响应丢失时进入 `delivery_unknown`，不自动重复注入。
4. delivered 与 applied 分开显示和持久化。
5. run 在 applied 前结束时，以同一 supplement 身份回到 Queue，不产生副本。
6. Supplement 不会同时作为下一 turn 再发送。
7. 历史加载和 SSE replay 得到相同的“前段 → 补充 → 后段 → 最终回答”顺序。

### 16.4 生命周期出口

覆盖：

- 正常完成；
- Agent/provider 错误；
- 用户取消；
- stream 替换；
- session 切换；
- 浏览器刷新；
- WebUI 进程重启；
- Gateway/local backend；
- worker teardown；
- 队列清空和 session 删除。

## 17. 验收标准

1. 用户在同一 active 会话中连续提交多条任务，后端全部持久接受并按用户确认的顺序串行启动。
2. 拖动、编辑、删除与自动 drain 并发时，不发生重复发送、错误删除、跨会话发送或静默丢失。
3. 页面刷新、会话切换、多标签页和服务恢复后，队列状态从后端收敛且不会依赖原页面内存。
4. 点击编辑后，任务原子退出可消费状态并回到 Composer；提交或放弃编辑均有确定恢复路径。
5. Queue Item 转 Supplement 后不会再作为普通下一 turn 重复发送。
6. 历史接口能够恢复用户补充信息及其 run/segment 所属关系，不依赖解析 `tool.content`。
7. WebUI 只有在 Agent 提供 applied 证据时才显示“已读取”；否则只显示“已收到”。
8. 同一 session 任意时刻最多一个 active Agent stream。

## 18. 非目标与待确认项

本方案不实现同一会话内多个 Agent turn 并行运行。需要真正并行执行时，应使用不同会话、后台任务或子 Agent，不应放宽 session 单 active-run 不变量。

待产品确认：

1. 编辑完成后保留原位置还是移动到队尾；本文默认保留原位置。
2. `editing` 队首是否阻塞后续任务；本文默认阻塞。
3. `delivery_unknown` 允许用户执行哪些恢复操作，以及是否需要管理员诊断入口。
4. 删除是否提供短时间撤销；本文仅要求内部 tombstone，不要求 UI 撤销。
5. Supplement 是否允许携带附件；如允许，需要先完成附件上传并把稳定路径交给 Agent，而不是保存浏览器 `File` 对象。
6. Queue Item 的模型/Profile 是否采用入队时快照；本文建议冻结 model/provider/workspace，Profile 切换需另行定义完整身份契约。
