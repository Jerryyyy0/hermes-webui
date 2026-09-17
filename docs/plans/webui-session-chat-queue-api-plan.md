# WebUI 会话消息 Queue 后端接口方案

**状态：** Proposed（仅设计，不代表已实现）

**范围：** 同一 WebUI 会话内的用户消息排队、排序、编辑、删除，以及将排队消息作为当前 Agent run 的补充指令发送。

**关联契约：** [`../rfcs/webui-pending-intent-controls.md`](../rfcs/webui-pending-intent-controls.md)、[`../rfcs/live-to-final-assistant-replies.md`](../rfcs/live-to-final-assistant-replies.md)、[`../rfcs/session-sse-contract-v1.md`](../rfcs/session-sse-contract-v1.md)。

**关联实现：** [`../../integration/pending_chat_turns.py`](../../integration/pending_chat_turns.py)、[`../../api/routes.py`](../../api/routes.py)、[`../../api/streaming.py`](../../api/streaming.py)、[`../../static/messages.js`](../../static/messages.js)。

## 1. 结论

第一期只保留三个入口：

```text
POST /api/chat/start          提交消息；能立即执行则启动，否则持久入队
GET  /api/chat/queue          查询会话的权威 Queue 快照
POST /api/chat/queue/commands 排序、更新、删除、转补充指令、显式启动队首
```

不新增独立入队接口，也不增加 `enqueue` action。普通消息统一提交到 `/api/chat/start`，由后端在 session lock 内原子决定立即启动或入队：

```text
session 空闲且 Queue 为空 → 200 running
session 忙碌或 Queue 非空 → 202 queued
```

入队和消费是两件事。后端可以接收并持久化 Queue Item，但不得在 worker teardown、cancel、服务恢复或后台扫描时主动消费用户 Queue。只有前端显式调用 `dispatch_next`，后端才能启动队首任务。

如果作为当前 Fork 的定制能力实现，两个新增接口映射为：

```text
GET  /api/integration/chat_queue
POST /api/integration/chat_queue/commands
```

本文后续使用较短的逻辑路径 `/api/chat/queue`。实现时只提供一组路径，不维护别名。

## 2. 必须成立的不变量

1. 后端 Queue 是唯一权威状态；浏览器存储只能作为渲染缓存。
2. 同一 session 任意时刻最多一个 active Agent run。
3. 新消息不能越过已经存在的 Queue Item。
4. 只有 `dispatch_next` 可以消费 `source=user_queue` 的项目。
5. 前端不能把 Queue Item 文本重新提交给 `/api/chat/start`；后端必须按 `entry_id` 原子领取已持久化项目。
6. 页面关闭或网络断开时，用户 Queue 保持等待，不自动执行。
7. 系统 wakeup 使用独立调度路径，不能借后台任务入口消费用户 Queue。
8. Supplement 的“已送达 Agent”和“模型已读取”必须分开表示。

## 3. 当前实现需要调整的地方

现有后端已经能在会话忙碌时把 `/api/chat/start` 消息保存到 `session.pending_next_turns`，也已有 `drain_pending_chat_turn()`。现有前端还有页面内 Queue 和 `setBusy(false)` 后重新发送文本的逻辑。

第一期需要：

- 把 `pending_next_turns` 升级为前后端共同使用的权威 Queue。
- 为 Queue 增加查询、排序、更新、删除和显式 dispatch 能力。
- 移除 cancel、worker teardown、服务恢复和 async delegation inbox 对用户 Queue 的自动 drain。
- 移除浏览器本地 `shift() + send()`；改为调用 `dispatch_next`。
- 保留系统 wakeup 的既有后端调度，但与 `source=user_queue` 明确隔离。

## 4. 数据模型

### 4.1 Queue 容器

第一期继续使用 Session 持久化，不新增数据库或第二份 Queue：

```text
session.queue_schema_version = 2
session.queue_revision = 7
session.pending_next_turns = [QueueItem, ...]
```

数组顺序就是权威顺序；`position` 只在 GET 响应中计算，不持久化。

### 4.2 Queue Item

```json
{
  "entry_id": "queue-001",
  "client_message_id": "01994580-6934-7d84-88d7-0d919be8e213",
  "text": "只分析登录模块，不要修改代码",
  "attachments": [],
  "execution": {
    "workspace": "/workspace/project",
    "model": "gpt-5",
    "model_provider": "openai",
    "profile": "default"
  },
  "source": "user_queue",
  "status": "queued",
  "item_version": 1,
  "created_at": 1789441200
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `entry_id` | 服务端生成的稳定 Queue Item ID |
| `client_message_id` | 逻辑消息 ID；新版前端生成，旧版调用缺失时由后端兼容补齐 |
| `text`、`attachments` | 已规范化的用户输入 |
| `execution` | 入队时解析完成的执行环境快照 |
| `source` | 普通消息固定为 `user_queue`，客户端不能覆盖 |
| `status` | Queue Item 当前状态 |
| `item_version` | 单项乐观并发版本 |
| `created_at` | 入队时间 |

入队时冻结 `execution`，避免用户随后切换模型、Profile 或 workspace，导致排队任务的执行环境静默变化。

历史 Queue Item 缺少 `client_message_id` 时必须继续正常读取，不能因为 schema 升级导致旧队列不可见或不可执行。兼容读取层使用现有 `entry_id` 补成稳定值 `legacy:<entry_id>`，并在项目下一次正常写回时持久化；不要求一次性重写全部历史 Session。

### 4.3 状态

```text
queued
 ├─→ dispatching ─→ sent
 ├─→ deleted
 ├─→ failed ──────→ queued
 └─→ supplement_delivering
       ├─→ supplement_delivered ─→ supplement_applied
       ├─→ queued
       └─→ delivery_unknown
```

- `queued`：允许排序、更新、删除、转 Supplement 或 dispatch。
- `dispatching`：正在启动新 run，不能再编辑或删除。
- `failed`：启动明确失败，保留内容供用户重试或删除。
- `delivery_unknown`：无法确认 Supplement 是否已经注入，禁止自动重试。
- `sent`、`deleted`、`supplement_applied`：终态，不计入待处理数量。

不设置 `editing` 状态、edit token 或 edit lease。点击编辑只把内容复制到 Composer；保存时调用原子 `update`，取消编辑只清理本地草稿。

## 5. `POST /api/chat/start`

### 5.1 请求

```json
{
  "session_id": "session-001",
  "message": "分析登录模块",
  "attachments": [],
  "model": "gpt-5",
  "model_provider": "openai",
  "workspace": "/workspace/project",
  "profile": "default",
  "client_message_id": "01994580-6934-7d84-88d7-0d919be8e213"
}
```

`session_id` 和 `message` 必填。新版前端必须为每次逻辑发送生成 `client_message_id`，同一次 HTTP 重试复用原值；但后端必须兼容历史版本前端缺少该字段，不能直接返回 `400`。

HTTP 边界按以下顺序归一化：

```text
存在 client_message_id
    → 使用 client_message_id

否则
    → 后端生成 legacy:<uuid>，继续接受请求
```

后端兜底生成的 ID 只保证本次请求能够正常处理；旧前端在响应丢失后重新发送时会得到新的 ID，因此不承诺“最多入队一次”。这项限制不影响正常发送和历史会话使用。

### 5.2 原子决策

后端在同一把 session lock 内重新读取 session，并执行：

```text
没有 active run 且 Queue 为空
    → 启动 Agent，返回 200 running

存在 active run 或 Queue 非空
    → 追加到队尾并保存，返回 202 queued
```

前端不预查 busy，也不在 `/start` 失败后补调另一个入队接口。这样可以避免“前端看到空闲，但请求到达时已经变忙”的竞态。

运行响应：

```json
{
  "status": "running",
  "session_id": "session-001",
  "stream_id": "stream-001",
  "client_message_id": "01994580-6934-7d84-88d7-0d919be8e213"
}
```

入队响应：

```json
{
  "status": "queued",
  "session_id": "session-001",
  "entry_id": "queue-001",
  "position": 1,
  "queue_revision": 7,
  "active_stream_id": "stream-000",
  "client_message_id": "01994580-6934-7d84-88d7-0d919be8e213"
}
```

响应始终返回归一化后的 `client_message_id`。新版前端提交相同 ID 重试时，后端返回第一次提交产生的结果，不重复启动或入队。入队结果可通过 Queue Item 去重；立即启动的结果必须在现有 pending turn/journal 中保存 `client_message_id → stream_id` 关联，不能只在 Queue 中查重。

## 6. `GET /api/chat/queue`

```http
GET /api/chat/queue?session_id=session-001
```

响应：

```json
{
  "session_id": "session-001",
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
    }
  ]
}
```

默认只返回仍需用户处理的项目，例如 `queued`、`failed` 和 `delivery_unknown`。前端在页面加载、会话切换、SSE 重连、收到 `queue_changed` 或命令冲突后重新获取该快照。

## 7. `POST /api/chat/queue/commands`

### 7.1 公共字段

```json
{
  "session_id": "session-001",
  "action": "reorder"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `session_id` | 是 | Queue 所属会话 |
| `action` | 是 | `reorder`、`update`、`delete`、`send_as_supplement` 或 `dispatch_next` |
| `expected_queue_revision` | 按 action | 整个 Queue 的乐观并发版本 |
| `entry_id` | 按 action | Queue Item ID |
| `expected_item_version` | 按 action | 单项乐观并发版本 |

第一期不增加通用 `command_id`。`reorder`、`update`、`delete` 和 `dispatch_next` 已由 revision、item version、状态机与 session lock 防止重复生效；Supplement 使用持久化的 `supplement_id` 标识投递。

### 7.2 版本从哪里获取

版本均由后端生成，前端不能自行计算：

- `expected_queue_revision` 来自 GET 响应顶层的 `queue_revision`。
- `expected_item_version` 来自目标项目的 `item_version`。
- 成功后采用命令响应中的新版本；响应未返回完整项目时重新 GET。
- 收到 `409` 后重新 GET，不在本地递增版本或自动覆盖。

编辑时，前端把 `entry_id + item_version` 与草稿绑定。即使期间收到 `queue_changed`，也不能静默替换草稿绑定的版本，否则会绕过冲突检测。

### 7.3 `reorder`

```json
{
  "session_id": "session-001",
  "action": "reorder",
  "expected_queue_revision": 7,
  "ordered_entry_ids": ["queue-002", "queue-001"]
}
```

`ordered_entry_ids` 必须无重复，并完整覆盖当前所有可排序的 `queued` 项。后端在 session lock 内校验 revision 后重排数组并递增 `queue_revision`。

### 7.4 `update`

```json
{
  "session_id": "session-001",
  "action": "update",
  "entry_id": "queue-001",
  "expected_item_version": 1,
  "text": "只分析登录模块，不要修改代码",
  "attachments": []
}
```

只允许更新 `queued` 项。成功后递增 `item_version` 和 `queue_revision`，并保持原位置。版本冲突返回 `409 queue_item_version_conflict`，前端保留本地草稿。

### 7.5 `delete`

```json
{
  "session_id": "session-001",
  "action": "delete",
  "entry_id": "queue-001",
  "expected_item_version": 1
}
```

只允许删除 `queued` 或 `failed` 项。项目已经进入 dispatch 或 Supplement 投递时返回 `409 queue_item_not_mutable`。

### 7.6 `dispatch_next`

```json
{
  "session_id": "session-001",
  "action": "dispatch_next",
  "expected_queue_revision": 7
}
```

请求不携带 `entry_id`。后端始终选择当前权威顺序中的第一条 `queued` 项；要优先执行其它任务，必须先成功 reorder。

后端流程：

1. 在 session lock 内校验 `expected_queue_revision`。
2. 确认不存在 active stream/run，且旧 worker 已退出 `ACTIVE_RUNS`。
3. 队首执行 `queued → dispatching`，保存后释放锁。
4. 使用 Queue Item 中已持久化的内容和执行环境启动 Agent。
5. 获得新 `stream_id` 后执行 `dispatching → sent`。
6. 启动明确失败时恢复为 `queued` 或标记 `failed`，不自动重试。

成功响应：

```json
{
  "accepted": true,
  "action": "dispatch_next",
  "status": "running",
  "entry_id": "queue-001",
  "stream_id": "stream-002",
  "queue_revision": 8
}
```

Queue 为空时返回 `status=empty`，不创建 stream。旧 run 尚未结算时返回 `409 session_not_ready`。

### 7.7 `send_as_supplement`

```json
{
  "session_id": "session-001",
  "action": "send_as_supplement",
  "entry_id": "queue-003",
  "expected_item_version": 2,
  "target_stream_id": "stream-000",
  "target_generation": 3
}
```

后端流程：

1. 在 session lock 内确认项目仍为 `queued`，目标 `stream_id + generation` 仍是当前 active run。
2. 执行 `queued → supplement_delivering`，生成并保存稳定 `supplement_id`。
3. 释放锁后调用 RuntimeAdapter 或 Agent `steer()`。
4. 明确未送达时恢复 `queued`；确认送达时进入 `supplement_delivered`。
5. Agent 证明下一次模型输入已经包含该内容时，进入 `supplement_applied`。
6. 无法确认是否送达时进入 `delivery_unknown`，禁止自动重试。

目标 run 已变化时返回 `409 active_stream_changed`，Queue Item 保持 `queued`。

## 8. 前端调度与事件

### 8.1 下一任务由前端触发

```text
后端：旧 run 完成并退出 ACTIVE_RUNS
        ↓
后端：持久化并发送 run_settled，不消费 Queue
        ↓
前端：GET Queue
        ↓
前端：根据产品策略或用户点击调用 dispatch_next
        ↓
后端：原子领取队首并启动 Agent
```

普通 `done` 只表示结果已经产生，不保证 worker teardown 完成。`run_settled` 才表示 session 可以接受 `dispatch_next`。如果重连期间错过事件，前端可以尝试 `dispatch_next`，并对 `409 session_not_ready` 做有限退避重试。

### 8.2 必须移除的自动消费点

以下路径不得启动 `source=user_queue` 的项目：

- `/api/chat/cancel` settle 后自动 drain；
- streaming worker teardown 自动 drain；
- 服务启动或恢复扫描；
- async delegation inbox；
- 浏览器 `setBusy(false)` 后本地 `shift() + send()`。

后端可以保留一个内部 dispatch 原语，但只能由通过认证和 CSRF 校验的 `dispatch_next` 调用。

### 8.3 Queue 与 Supplement 事件

复用会话级 SSE，新增或明确以下可回放事件：

```text
queue_changed
run_settled
supplement_delivered
supplement_applied
supplement_fallback_queued
```

事件必须先持久化、后广播。

- `supplement_delivered`：只能显示“已收到用户补充指令，继续处理中”。
- `supplement_applied`：可以显示“上一阶段已完成 · 已读取用户补充指令”。

“上一阶段已完成”只表示补充前的可视 assistant segment 已结束，不表示整个 run 已结束。

### 8.4 历史展示

Queue Item 创建时不能直接写成普通 `Session.messages.role=user`，否则尚未执行的任务可能进入 Agent 上下文。

建议继续分开保存：

```text
pending_next_turns  待执行 Queue
turn_interventions Supplement 及其投递状态
Session.messages    正常 Agent/provider 历史
```

历史接口把 `turn_interventions` 投影为带 `kind=supplement` 的用户可见消息。该投影只用于展示，不能再次作为普通下一 turn 喂给 Agent，也不能依赖解析 `tool.content` 中的 out-of-band 标记。

## 9. 并发与错误处理

### 9.1 锁范围

所有 Queue 读改写使用 `_get_session_agent_lock(session_id)` 或其统一服务封装。锁内完成 session 重读、状态和版本校验、状态修改及 `session.save()`；锁内不得等待 Agent、Provider、SSE、文件上传或 worker 退出。

### 9.2 Revision

- `queue_revision`：Queue 可见内容、顺序或状态变化时递增。
- `item_version`：单个项目内容或状态变化时递增。
- 多标签页提交旧版本时返回 `409`，不能覆盖新状态。

### 9.3 错误码

| HTTP | error | 场景 |
| --- | --- | --- |
| `400` | `invalid_queue_command` | 字段或 action 非法 |
| `404` | `session_not_found` | session 不存在或不可见 |
| `404` | `queue_item_not_found` | Queue Item 不存在 |
| `409` | `queue_revision_conflict` | Queue 已变化 |
| `409` | `queue_item_version_conflict` | Queue Item 已变化 |
| `409` | `queue_item_not_mutable` | 项目已进入不可修改状态 |
| `409` | `session_not_ready` | session 仍有 active run 或 worker 未结算 |
| `409` | `active_stream_changed` | Supplement 目标 run 已变化 |
| `409` | `supplement_delivery_unknown` | 无法确认 Supplement 是否已注入 |
| `422` | `supplement_unsupported` | 当前 runtime 不支持 Steer |
| `500` | `queue_persist_failed` | Queue 持久化失败 |

面向调用方的错误说明使用中文。接口沿用现有 cookie 认证和 CSRF 约定，并校验 session 属于当前可见 Profile。

## 10. 前端接入

1. 会话加载、切换、重连和 `queue_changed` 后调用 GET Queue。
2. 普通发送统一调用 `/api/chat/start`；`200 running` 连接 stream，`202 queued` 更新 Queue UI。
3. 拖动结束后提交完整 ID 顺序。
4. 编辑时把文本和 `item_version` 放入 Composer；保存调用 `update`，取消只清理本地草稿。
5. 删除成功后从本地快照移除，冲突时重新 GET。
6. 收到 `run_settled` 后，根据产品策略自动调用 `dispatch_next`，或等待用户点击“执行下一条”。
7. `dispatch_next` 成功后连接返回的 `stream_id`。
8. Supplement 按 delivered/applied 两阶段显示。

Queue 卡片保留拖动、编辑、删除和“作为补充信息发送”操作。窄屏可以把低频操作放入单项菜单。`failed` 和 `delivery_unknown` 必须有可见状态，不能只写日志。

## 11. 实现边界

Fork 特有实现放在：

```text
integration/chat_queue/
  models.py
  service.py
  handlers.py
  projection.py

integration/tests/chat_queue/
  test_handlers.py
  test_service.py
  test_concurrency.py
  test_supplement.py
```

上游接缝只保留薄调用：

- `api/routes.py`：integration 路由分发和 `/api/chat/start` 入队钩子。
- `api/streaming.py`：发出 `run_settled` 及 Supplement 事件，不消费用户 Queue。
- `static/messages.js`：调用接口并渲染 Queue；复杂 Fork UI 放在 `integration/assets/`。

新增 integration HTTP 接口时同步更新 `integration/swagger/openapi.json`、`integration/README.md` 和 `integration/CHANGELOG.md`。

## 12. 实施顺序

### 第一阶段：Queue

- 扩展 `pending_next_turns`，增加 `queue_revision` 和 `item_version`。
- 实现 GET Queue 及 `reorder`、`update`、`delete`、`dispatch_next`。
- 让 `/api/chat/start` 和 `dispatch_next` 复用同一个 Queue Service。
- 移除所有用户 Queue 自动 drain 和浏览器本地 `shift() + send()`。
- 增加持久化 `queue_changed` 和 `run_settled`。

### 第二阶段：Supplement

- 实现 `send_as_supplement` 和稳定 `supplement_id`。
- 持久化 `turn_interventions`，支持历史恢复。
- Agent/runtime 先提供 `supplement_applied` 证据，再启用“已读取用户补充指令”文案。

## 13. 测试与验收

至少覆盖：

1. session 空闲且 Queue 为空时 `/start` 立即启动。
2. session 忙碌或 Queue 非空时 `/start` 按顺序入队。
3. 新版前端相同 `client_message_id` 重试不重复启动或入队。
4. 旧版前端不发送 `client_message_id` 时仍能正常提交，响应返回后端生成的 `legacy:<uuid>`。
5. 历史 Queue Item 缺少 `client_message_id` 时仍能查询、更新、删除和 dispatch，写回后获得稳定的 `legacy:<entry_id>`。
6. 刷新、切换会话和多标签页读取相同 Queue。
7. reorder、update、delete 与 dispatch 并发时最多一个操作基于匹配版本成功。
8. 两个标签页同时 `dispatch_next` 时最多启动一个 run。
9. Queue 为空时 `dispatch_next` 不创建 stream。
10. 正常完成、错误、取消、worker teardown、浏览器关闭和服务重启都不会自动消费用户 Queue。
11. Supplement 只投递给匹配的 `stream_id + generation`，不会同时作为下一 turn 重复发送。
12. 历史恢复保持“原执行内容 → 用户补充 → 继续执行 → 最终回答”的顺序。
13. 只有收到 `supplement_applied` 才显示“已读取用户补充指令”。
14. 同一 session 任意时刻最多一个 active Agent run。

## 14. 非目标与待确认项

本方案不实现同一 session 内多个 Agent turn 并行运行，也不在第一期新增独立数据库、分布式 Queue 或多 WebUI worker 调度。

待产品确认：

1. 收到 `run_settled` 后，前端默认自动调用 `dispatch_next`，还是等待用户点击“执行下一条”。
2. `delivery_unknown` 允许用户执行“保留、重新排队或放弃”中的哪些操作。
3. Supplement 第一阶段是否只支持文本；本文默认不支持附件。
