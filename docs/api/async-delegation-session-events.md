# 异步委派批次的会话事件接口交互

- **状态：** 已实施；child task 的逐项实时执行进度不在本协议范围内，批次完成后仅提供汇总。
- **范围：** `delegate_task(mode="background")` 的服务端事件契约、后台完成、wakeup 回复与会话级 SSE 资源释放；不约束任何特定 WebUI 前端实现。

本文定义目标接口交互，不改变 Hermes Agent 的 `delegate_task` 工具、异步委派数据库或 completion 事件格式。

一次 `delegate_task(mode="background")` 对 WebUI 来说是一个 delegation 批次：
`delegation_id` 标识整个批次，不标识批次内的某个 child task。批次可以包含多个并行
child task；Agent 等所有 child task 都结束后只投递一条 completion，WebUI 只为该批次
启动一条 wakeup stream。child task 的逐项结果只通过 `child_task_summary` 汇总到生命周期
事件，不会拆成多个 `delegation_id` 或多个 wakeup stream。

外部前端的调用顺序、事件消费和重连处理，请参见
[异步委派：外部前端接入指南](async-delegation-external-client-guide.md)。

> 本文描述的事件均由 WebUI 服务端产生；不要求 Hermes Agent 修改 `delegate_task` 或 completion 事件格式。

## 实现状态

目标异步委派生命周期事件由 sidecar 的持久化 `async_delegation_activity_version` 编号。每个状态变更先持久化，再构造统一事件信封并发射：

| 事件 | 发射时机 | 目标发射目标 |
| --- | --- | --- |
| `background_task_dispatched` | 成功记录派发归属后 | 仅派发它的当前 chat stream；会话 SSE 不重复发送。 |
| `background_tasks_snapshot` | 每次 session SSE 建连、完成 sidecar 读取后 | 仅发往新建立的 session SSE；无论是否存在未结算任务都发送，用于填补建连竞态和页面刷新恢复。 |
| `background_task_status` | completion 进入 `queued` / `running`，或 wakeup 进入 `settled` / `failed` | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `bg_task_complete` | 服务端已成功接受 completion；wakeup 可能已启动，也可能仍为 `queued` | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `server_turn_started` | 服务端成功创建 wakeup run | `SessionChannel`。 |
| `background_task_unresolved` | completion 耗尽归属重试，无法安全定位 origin turn | `SessionChannel`。 |
| `background_tasks_idle` | sidecar 中最后一个任务及其 wakeup 结算后 | 匹配的活跃聊天流，以及 `SessionChannel`。 |

`GET /api/sessions/{session_id}/events` 同时保留 run-journal 回放、活跃 run 订阅和 snapshot 恢复，并原子订阅 `SessionChannel`。含未结算后台工作的连接会跨 Agent run 继续接收生命周期事件；普通会话保留原有的 run 终态关闭行为。

`background_tasks_snapshot` 在每次成功建连后发送，即使其活动计数均为 `0`。外部客户端可据此在刷新或重连后立即判断是否关闭自己的会话 SSE；服务端不会因为空快照而截断既有 run-journal 的恢复能力。

会话级取消屏障、origin user message 的 `async_delegations` 投影和 `background_task_unresolved` 已持久化并对外发射。取消范围内的 completion 只会收口状态，不能唤起新的 Agent stream。

实现不另起第二条客户端 transport，也不把 assistant token 转移到会话事件流。异步委派生命周期事件使用第 5.2 节定义的统一事件信封。

## 1. 目标与边界

一个会话可以派发多个 delegation 批次；每个批次又可以包含多个后台 child task，用户也可以切换到其他会话继续对话。一次父聊天 turn 中每调用一次 `delegate_task(mode="background")`，就会产生一个独立批次，因此同一条父 chat stream 可能连续收到多个 `background_task_dispatched`。目标是：

- 只有含未结算后台工作的会话才保持会话级 SSE；
- delegation 批次完成后，服务端自行启动 parent wakeup turn；
- wakeup 回复仍通过独立的聊天流发送，绝不把 token 混入会话事件流；
- 一个会话同一时刻只运行一个 Agent turn；
- 多个会话可各自持有后台任务订阅；切换聊天页不应使其他会话丢失完成通知；
- 最后一个 delegation 批次及其 wakeup 都结算后，服务端发出“可关闭”的聚合事件。

这里的“后台任务结算”包含两层：

| 层 | 字段 | 完成条件 |
| --- | --- | --- |
| delegation 批次执行 | `status` | `completed`、`failed` 或 `cancelled` |
| 父会话处理 | `wakeup_state` | `settled` 或 `failed` |

仅 `status=completed` 不能释放会话事件流：该 delegation 批次的 wakeup 可能仍在排队或生成回复。

## 2. 接口职责

| 接口 | 生命周期 | 职责 |
| --- | --- | --- |
| `POST /api/chat/start` | 每个用户 turn 一次 | 启动正常聊天 run，返回 `stream_id`。 |
| `GET /api/chat/stream?stream_id=…` | 每个 Agent run 一条 | 接收该 run 的 token、工具、`done`、`stream_end`。后台 wakeup 的回复也走这里。 |
| `GET /api/sessions/{session_id}/events` | 每个存在未结算后台任务的 session 一条 | 接收后台任务生命周期及服务端启动新 run 的通知；新的实时 assistant token 不承载于此，仍以 chat stream 为准。 |
| `GET /api/session?session_id=…` | 重连/切换时按需调用 | 用持久化 session 结果恢复显示，作为 SSE 缺口或已完成 run 的兜底。 |
| `POST /api/sessions/background_tasks/cancel` | 用户显式取消时 | 在请求体中指定 session，快照并取消该 session 当时全部未结算 delegation；不取消主会话或之后新派发的任务。 |
| `GET /api/sessions/background_tasks/cancel?session_id=…` | 查询取消请求的收口状态 | 读取该 session 当前或最近一次取消记录；用于轮询确认取消已完成。 |

`GET /api/sessions/events` 是全局 session-list invalidation 流，不能替代按会话的 `GET /api/sessions/{session_id}/events`。

### 2.1 历史会话查询与每轮任务状态

历史查看不依赖仍然存活的 session SSE。调用 `GET /api/session?session_id={session_id}` 后，在返回的 `messages[]` 中查找带 `_turn_key` 的真实 `role: "user"` 消息；该消息的可选 `async_delegations` 是该轮派发任务的唯一历史状态。

```json
{
  "role": "user",
  "content": "请在后台完成调研",
  "_turn_key": "turn:8",
  "async_delegations": {
    "state": "settled",
    "items": {
      "deleg_123": {
        "status": "completed",
        "wakeup_state": "settled",
        "cancel_state": "none",
        "child_task_count": 2,
        "goals": ["调研模型 A", "调研模型 B"],
        "child_task_summary": {
          "total": 2,
          "completed": 2,
          "failed": 0,
          "cancelled": 0
        },
        "dispatched_at": 1786004400,
        "completed_at": 1786004410
      }
    }
  }
}
```

`items` 的键就是 `delegation_id`。每个 item 的 `status`、`wakeup_state`、`cancel_state`、`child_task_count`、`goals`、`child_task_summary`、`dispatched_at` 和 `completed_at` 与同批次生命周期事件含义一致；除 `child_task_summary` 与 `completed_at` 外均必填，未请求取消时历史 `cancel_state` 固定为 `none`。SSE 事件中省略 `cancel_state` 与该值等价。

`_turn_key` 与事件中的 `origin_turn_key` 必须相同，用于把实时事件与历史消息关联。对本契约上线后创建的 user turn，`async_delegations` 缺失表示未成功派发后台任务。旧会话、导入会话或无法确认写入版本的消息字段缺失时必须视为未知，不能推断为未派发。

`async_delegations.state` 是**这一条 user message**下全部 delegation 的聚合结论，不是整个
session 的取消状态。具体结果始终以 `items[delegation_id]` 的 `status`、`wakeup_state` 与
`cancel_state` 为准：

| 聚合 `state` | 判定条件 | 含义 |
| --- | --- | --- |
| `running` | 仍有批次 `status=running`，或其 `wakeup_state` 为 `idle` / `queued` / `running`，且没有任何批次处于 `cancel_state=requested`。 | 该轮仍有正常后台工作或 wakeup 未结算。 |
| `cancelling` | 仍有未结算批次，且至少一个批次 `cancel_state=requested`。 | 已对该轮覆盖的批次请求取消，尚未全部收口。 |
| `cancelled` | 全部批次已结算，且全部 item 的 `status=cancelled`。 | 该轮的全部后台批次都实际被取消。 |
| `settled` | 全部批次已结算，但并非全部 item 的 `status=cancelled`。 | 该轮已经结束，包含成功、失败，或取消竞争中仍完成的混合结果。 |

例如，两个批次都被 Agent 中断时，`items` 中二者均为
`status=cancelled`、`wakeup_state=settled`、`cancel_state=cancelled`，该轮聚合状态为
`cancelled`。若其中一个批次已在取消竞争中正常完成，其状态保留
`status=completed`、`cancel_state=requested`；即使另一个批次被取消，该轮聚合状态也必须为
`settled`，不能写成 `cancelled`。

这与 `GET /api/sessions/background_tasks/cancel` 的响应 `state=settled` 不同：后者表示一次
**session 级固定取消范围**已经收口，允许其中包含 `completed`、`failed` 或 `cancelled` 的
真实终态；它不等价于某一条 user message 的 `async_delegations.state`。

历史查询只读取持久化结果，不补发 `background_task_dispatched`、`server_turn_started` 或 token。若某个 wakeup 已经结束，客户端用该轮 `async_delegations` 和可见消息恢复结果；只有仍在运行的任务才需要继续订阅 session SSE。

## 3. 完整交互

```mermaid
sequenceDiagram
    participant Subscriber as SSE subscriber
    participant Chat as /api/chat/start + chat stream
    participant Agent as Hermes Agent
    participant Sidecar as WebUI session sidecar
    participant Events as /api/sessions/{id}/events

    Subscriber->>Chat: POST /api/chat/start
    Chat-->>Subscriber: { stream_id: stream_1 }
    Subscriber->>Chat: GET /api/chat/stream?stream_id=stream_1
    Chat->>Agent: run parent turn
    Agent-->>Chat: tool_complete(delegate_task, dispatched)
    Chat->>Sidecar: delegation_id -> origin_turn_key; status=running
    Chat-->>Subscriber: background_task_dispatched (session_id)
    Subscriber->>Events: GET /api/sessions/{session_id}/events
    Events-->>Subscriber: background_tasks_snapshot（sidecar 当前状态）
    Chat-->>Subscriber: done / stream_end (原父 turn)

    Agent-->>Sidecar: async_delegation completion
    Sidecar-->>Events: background_task_status（一个 batch completion）
    alt session 空闲
        Sidecar->>Chat: 服务端启动 async_delegation_wakeup
    else session 有活跃 turn
        Sidecar-->>Events: wakeup_state=queued
        Note over Sidecar,Chat: 当前 turn 终态后按完成顺序启动 wakeup
        Sidecar->>Chat: 服务端启动 async_delegation_wakeup
    end
    Chat-->>Events: server_turn_started { stream_id: stream_2 }
    Subscriber->>Chat: GET /api/chat/stream?stream_id=stream_2
    Chat-->>Subscriber: token / tool / done / stream_end（后台回复）
    Sidecar-->>Events: background_task_status wakeup_state=settled
    opt 所有 delegation 批次均已结算
        Sidecar-->>Events: background_tasks_idle
        Subscriber->>Events: close SSE connection
    end
```

## 4. 正常聊天入口与按需订阅

### 4.1 用户发起聊天

```http
POST /api/chat/start
Content-Type: application/json

{
  "session_id": "session_123",
  "message": "请把调研任务放到后台执行",
  "model": "provider/model",
  "model_provider": "provider",
  "workspace": "/workspace/project",
  "profile": "default"
}
```

成功后最小响应：

```json
{
  "session_id": "session_123",
  "stream_id": "stream_parent_1",
  "pending_started_at": 1786004400
}
```

调用方随后打开：

```text
GET /api/chat/stream?stream_id=stream_parent_1
```

### 4.2 派发通知与会话 SSE 建连

`delegate_task` 的工具结果只供后端完成归属校验与 sidecar 写入；SSE 订阅方不读取、不解析 `tool_complete`，也不以工具结果决定是否建立会话 SSE。

后端在成功写入 `delegation_id → origin_turn_key` sidecar 映射后，必须向**同一条仍处于连接状态的 chat SSE**发出专用 `background_task_dispatched` 事件。订阅方从事件顶层的 `session_id` 生成固定路径，建立会话级事件流：

```text
GET /api/sessions/session_123/events
Accept: text/event-stream
```

这是首次建连的唯一触发条件。因为通知走的是已连接的 chat SSE，它不会出现“必须先订阅 session SSE 才能收到建立 session SSE 的通知”的循环。

同一 session 的多个 delegation 批次共用一条 SSE 连接。订阅方不能为每个 `delegation_id` 或 child task 建一条连接。多个批次的生命周期事件在这条连接中按服务端发射顺序串行发送，并通过 `background_activity_version` 标识 session 内的状态版本。

为覆盖 chat SSE 重连，`background_task_dispatched` 必须遵循该聊天流的恢复/重放语义：在父 turn 结束前重连时，服务端必须再次交付尚未确认的该事件。订阅方仍不回退解析 `tool_complete`。session SSE 的第一个业务帧必须是 `background_tasks_snapshot`，以 sidecar 当前状态消除“派发通知已送达、但 session SSE 尚未连上时任务状态发生变化”的竞态。

## 5. 会话事件流

### 5.1 连接与通用规则

```http
GET /api/sessions/{session_id}/events
Accept: text/event-stream
Last-Event-ID: <可选，最后已处理的 event id>
```

- 订阅方以 `session_id` 为 key 管理连接；同一订阅方对同一 session 最多一条订阅。
- 事件只描述其所属 session 的后台状态；呈现、通知及跨会话处理均由具体接入方决定。
- 重连后订阅方按事件 ID 去重；无法安全回放时，服务端发送 session snapshot，订阅方用 `GET /api/session` 或 snapshot 重建本地状态。
- 即使没有订阅方连接，后台任务和 server-side wakeup 仍必须继续执行；SSE 仅是观察通道。

### 5.2 统一事件信封

所有按会话 SSE 的后台任务生命周期及建连快照事件都使用同一 JSON 顶层形状。SSE 的 `event:` 行与 `event_type` 值相同，SSE 的 `id:` 与 JSON 中的 `event_id` 值相同。`background_task_dispatched` 是普通 chat stream 的专用事件，不属于按会话 SSE；它仍使用该 stream 的 run-journal cursor 作为 SSE `id:`，其 JSON `event_id` 用于业务去重。

```text
event: <event_type>
id: <event_id>
data: {
  "schema_version": 1,
  "event_id": "opaque-id",
  "event_type": "background_task_status",
  "session_id": "session_123",
  "emitted_at": 1786004400,
  "background_activity_version": 12,
  "payload": {}
}
```

公共字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | integer | 固定为 `1`；未来不兼容变更才升级。 |
| `event_id` | string | 对订阅方不透明的唯一事件标识；用于 SSE 重连和去重。 |
| `event_type` | string | 与 SSE `event:` 完全一致。 |
| `session_id` | string | 事件所属 WebUI 会话。 |
| `emitted_at` | number | 服务端发射时的 Unix 时间戳。 |
| `background_activity_version` | integer | session sidecar 中单调递增的后台活动版本；订阅方忽略早于本地已处理版本的状态。 |
| `payload` | object | 仅包含该事件专属字段。 |

`delegation_id`、`origin_turn_key`、`stream_id`、`status`、`wakeup_state` 和 `active_task_count` 不放在公共顶层。它们只在语义相关的事件 `payload` 中出现，避免空字段被误解为真实状态。

delegation 生命周期事件（`background_task_status`、`bg_task_complete`、`server_turn_started`）的 `payload` 必须共同包含以下字段。chat stream 的 `background_task_dispatched` 复用这组字段，但只用于提示订阅方建立或复用会话 SSE，不能作为会话任务状态基线：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `delegation_id` | string | 后台委派批次唯一标识；批次内的 child task 共享此 ID。 |
| `child_task_count` | integer | 该批次启动的 child task 数量。 |
| `goals` | string[] | child task 的目标列表；可能为空。 |
| `origin_turn_key` | string | 派发该 delegation 的真实 user turn。 |
| `status` | string | delegation 批次执行状态：`running`、`completed`、`failed` 或 `cancelled`。 |
| `wakeup_state` | string | 父会话处理状态：`idle`、`queued`、`running`、`settled` 或 `failed`。 |
| `child_task_summary` | object，可选 | completion 后的逐项结果计数；不包含 child 结果正文。 |
| `cancel_state` | string，可选 | `requested` 或 `cancelled`；缺失等同于未请求取消。 |

`server_turn_started` 在这些字段之外增加 `stream_id` 和 `source`；`background_tasks_snapshot.payload.tasks[]` 复用同一组 delegation 字段。`background_tasks_idle` 是会话聚合事件，不携带某一个 delegation 字段。

`child_task_summary` 的形状为：

```json
{
  "total": 2,
  "completed": 1,
  "failed": 1,
  "cancelled": 0
}
```

Agent completion 的顶层 `status=error` 会转换为 WebUI 事件的 `status=failed`。如果批次中
只有部分 child task 失败，仍以 Agent 给出的批次状态为准，并用 `child_task_summary` 表示
逐项结果。订阅方不能根据某一个 child 的结果自行创建或关闭 stream。

### 5.2.1 服务端桥接

`GET /api/sessions/{session_id}/events` 的 handler 同时拥有两类订阅：

```text
run journal / active StreamChannel
  → 保留既有恢复/回放、run journal cursor、session_snapshot；实时 assistant token 仍以 chat stream 为准

SessionChannel(session_id)
  → background_task_status
  → bg_task_complete
  → server_turn_started
  → background_task_unresolved
  → background_tasks_idle
```

桥接要求：

1. 先完成 session 可见性与认证校验，再以 `session_id` 原子订阅 `SessionChannel`。
2. 从订阅成功到 HTTP SSE 头、初始恢复和循环写入的所有路径，都必须被同一个 `try/finally` 覆盖；每次退出均执行 `SessionChannel.unsubscribe()`。
3. journal replay 继续使用既有 `Last-Event-ID` / snapshot 语义；`SessionChannel` 的瞬时生命周期事件不伪造为 run-journal token，也不能假称已被 journal 精确重放。
4. `background_task_dispatched` 只由发起该 delegation 的 chat stream 发送，`SessionChannel` 不重复发送该事件。会话状态由建连后的 `background_tasks_snapshot` 和后续生命周期事件提供。
5. `server_turn_started` 只通知订阅方附着既有 `stream_id`；订阅方不得据此再次调用 `POST /api/chat/start`。
6. handler 必须在开始 drain `SessionChannel` 队列前写出 `background_tasks_snapshot`。快照读取与订阅之间发生的状态变化要么进入快照，要么留在队列中作为快照后的增量；不得在快照前写出生命周期事件。

### 5.3 `background_task_dispatched`

该事件只表示后台委派已被后端接受、归属已持久化；它通知派发该任务的订阅方建立或复用 session SSE，不是任务实际完成的信号。

服务端发射顺序必须为：

```text
delegate_task 返回 dispatched
  → 校验 delegation_id
  → 持久化 delegation_id → origin_turn_key sidecar 映射
  → 成功后向当前 chat stream 发 background_task_dispatched
```

sidecar 写入失败时不得发事件。这样订阅方不会收到一个服务端无法定位、恢复或结算的后台任务。

```text
event: background_task_dispatched
id: deleg_123:dispatched:12
data: {
  "schema_version": 1,
  "event_id": "deleg_123:dispatched:12",
  "event_type": "background_task_dispatched",
  "session_id": "session_123",
  "emitted_at": 1786004400,
  "background_activity_version": 12,
  "payload": {
    "delegation_id": "deleg_123",
    "child_task_count": 2,
    "goals": ["调研模型 A", "调研模型 B"],
    "origin_turn_key": "turn:8",
    "status": "running",
    "wakeup_state": "idle",
    "dispatched_at": 1786004400
  }
}
```

该事件只发往当前 chat stream，用于首次建立或复用会话 SSE。订阅方以顶层 `session_id` 生成 `/api/sessions/{session_id}/events`，无需后端下发 URL。会话 SSE 不得重复发送该事件；无论连接建立时任务处于何种状态，均由首个 `background_tasks_snapshot` 给出权威基线。

订阅方不读取或解析 `tool_complete`。chat SSE 断线后，服务端必须按第 4.2 节重放尚未确认的 `background_task_dispatched`；连接成功后的 `background_tasks_snapshot` 是后台任务状态的最终依据。

### 5.4 `background_tasks_snapshot`

每次 session SSE 成功完成认证、可见性校验和 `SessionChannel` 订阅后，服务端立即从该 session 的 sidecar 构建并只向这个新订阅者发送一次快照，**无论是否存在未结算后台任务**。它解决以下竞态：

```text
chat stream 收到 background_task_dispatched
  → 订阅方开始建立 session SSE
  → delegation 批次在 HTTP 建连期间完成或进入 queued
  → snapshot 返回当前记录，订阅方不依赖是否错过中间事件
```

```text
event: background_tasks_snapshot
id: background:snapshot:12
data: {
  "schema_version": 1,
  "event_id": "background:snapshot:12",
  "event_type": "background_tasks_snapshot",
  "session_id": "session_123",
  "emitted_at": 1786004405,
  "background_activity_version": 12,
  "payload": {
    "active_task_count": 1,
    "active_delegation_count": 1,
    "active_child_task_count": 2,
    "tasks": [
      {
        "delegation_id": "deleg_123",
        "child_task_count": 2,
        "goals": ["调研模型 A", "调研模型 B"],
        "origin_turn_key": "turn:8",
        "status": "running",
        "wakeup_state": "idle",
        "dispatched_at": 1786004400,
        "completed_at": null
      }
    ]
  }
}
```

`tasks` 只包含未结算 delegation 批次，即 `status=running` 或 `wakeup_state` 为 `idle`、`queued`、`running` 的记录。订阅方以快照完整替换该 session 的后台委派本地集合；后续只接受 `background_activity_version` 不早于该快照的增量事件。`active_task_count` 是兼容字段，统计 delegation 批次数；新的接入方应使用 `active_delegation_count` 与 `active_child_task_count`。

空快照必须使用相同信封，并令 `tasks: []`、`active_delegation_count: 0`、`active_child_task_count: 0`。它是刷新恢复时的关闭结论：客户端可关闭这条刚建立的 SSE 并删除持久化的待跟踪 session ID，不必等待刷新前可能已错过的 `background_tasks_idle`。

### 5.5 `background_task_status`

表示某个 `delegation_id` 的持久化生命周期变化，不创建新的聊天气泡。多个 delegation 批次的事件会在同一条 session SSE 中交错出现；订阅方必须按 `delegation_id` 更新对应批次，不能把它们合并成一个任务。

```text
event: background_task_status
id: deleg_123:status:13
data: {
  "schema_version": 1,
  "event_id": "deleg_123:status:13",
  "event_type": "background_task_status",
  "session_id": "session_123",
  "emitted_at": 1786004410,
  "background_activity_version": 13,
  "payload": {
    "delegation_id": "deleg_123",
    "child_task_count": 2,
    "goals": ["调研模型 A", "调研模型 B"],
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "queued",
    "child_task_summary": {
      "total": 2,
      "completed": 1,
      "failed": 1,
      "cancelled": 0
    }
  }
}
```

`payload` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `delegation_id` | string | 是 | 后台委派批次唯一标识。 |
| `child_task_count` | integer | 是 | 批次中的 child task 数量。 |
| `goals` | string[] | 是 | 批次中各 child task 的目标；没有目标时为空数组。 |
| `origin_turn_key` | string | 是 | 成功派发该 delegation 的真实 user turn。 |
| `status` | string | 是 | `running`、`completed`、`failed`、`cancelled`。 |
| `wakeup_state` | string | 是 | `idle`、`queued`、`running`、`settled`、`failed`。 |
| `child_task_summary` | object | 否 | completion 后的 child 结果计数。 |
| `cancel_state` | string | 否 | `requested` 或 `cancelled`；缺失表示未请求取消。 |

收到 `status=completed, wakeup_state=queued` 时，订阅方必须继续保留该 session 的 SSE 连接；这不是可关闭状态。

### 5.5.1 会话级取消

客户端通过以下接口显式取消该 session 在请求瞬间的全部未结算 delegation：

```http
POST /api/sessions/background_tasks/cancel
Content-Type: application/json

{
  "session_id": "session_123"
}
```

`session_id` 必填。服务端必须在 session agent lock 内读取未结算 delegation，并先持久化下列取消屏障，再向 Agent 请求中断：

```json
{
  "async_delegation_cancellation": {
    "state": "cancelling",
    "delegation_ids": ["deleg_123", "deleg_456"],
    "requested_at": 1786004500
  }
}
```

取消屏障属于 Session sidecar，不挂到主会话的普通消息状态。它必须跨 HTTP 请求、completion 和服务端重启保留。新派发也必须在同一把 lock 内写入，因此不会落入已持久化的 `delegation_ids`。

若该 session 已处于 `cancelling`，重复调用返回该屏障的既有范围，不得重新扫描或扩大范围。范围内全部批次收口后，服务端写入 `state: "settled"` 与 `settled_at`；下一次取消在 lock 内原子替换为新的快照。

```json
{
  "session_id": "session_123",
  "state": "cancelling",
  "delegation_ids": ["deleg_123", "deleg_456"],
  "requested_at": 1786004500,
  "settled_at": null
}
```

目标接口的响应分支如下：

| 条件 | HTTP 状态 | 响应语义 |
| --- | --- | --- |
| 成功创建或复用取消屏障 | `202` | `state: "cancelling"` 与固定的 `delegation_ids`。 |
| 当前没有未结算 delegation | `200` | `state: "idle"`、`delegation_ids: []`；未创建取消屏障。 |
| 缺少或无效 `session_id` | `400` | 请求未生效。 |
| session 不存在或调用方不可见 | `404` | 请求未生效，且不得泄露会话信息。 |
| 无法持久化取消屏障 | `500` | 请求未生效；不得先请求 Agent 中断。 |

HTTP `202` 只表示服务端已持久化取消范围并接受中断请求，不表示 child 已中断。服务端同时把每个受影响 origin user message 的 `async_delegations.state` 写为 `cancelling`，再向 Agent 请求中断。关闭 SSE、页面刷新或网络断开绝不触发这个接口。

客户端不得轮询 `POST`。它可能在既有取消已收口后开启新的取消周期；确认本次取消状态必须读取下面的 `GET` 接口：

```http
GET /api/sessions/background_tasks/cancel?session_id=session_123
```

```json
{
  "session_id": "session_123",
  "state": "settled",
  "delegation_ids": ["deleg_123", "deleg_456"],
  "requested_at": 1786004500,
  "settled_at": 1786004502
}
```

`GET` 读取 Session sidecar 中当前或最近一次取消记录，不触发取消。`state` 只能为 `cancelling`、`settled` 或 `idle`：`cancelling` 表示固定范围尚有批次未收口；`settled` 表示该响应中的全部 `delegation_ids` 已收口，是取消完成的唯一接口判断；`idle` 表示该 session 没有可查询的取消记录。`idle` 响应的 `delegation_ids` 为空，两个时间字段为 `null`。

| 条件 | HTTP 状态 | 响应语义 |
| --- | --- | --- |
| 有进行中或最近已收口的取消记录 | `200` | 返回该记录及其 `state`。 |
| 没有取消记录 | `200` | 返回 `state: "idle"` 与空 `delegation_ids`。 |
| 缺少或无效 `session_id` | `400` | 查询未执行。 |
| session 不存在或调用方不可见 | `404` | 查询未执行，且不得泄露会话信息。 |

取消流程不新增专用 SSE 事件。已连接的 session SSE 仍可发送既有 `background_task_status`、`bg_task_complete` 和 `background_tasks_idle`，以便实时更新界面；它们都不是本次取消完成的权威判断。尤其是新派发任务仍在运行时，取消记录可以已经 `settled`，而 `background_tasks_idle` 不会出现。

每个受影响批次通过既有 `background_task_status` 收口，不新增 SSE 事件。Agent 确认中断时：

```json
{
  "delegation_id": "deleg_123",
  "origin_turn_key": "turn:8",
  "status": "cancelled",
  "wakeup_state": "settled",
  "cancel_state": "cancelled"
}
```

取消不能逆转恰好已完成的 child。该批次保留真实的 `status=completed` 或 `failed`，
`cancel_state=requested` 表示它属于取消范围但未能被中断；该轮状态收口为 `settled`。
取消范围内的晚到 completion 必须被确认但不得启动 wakeup，因而不得发送
`server_turn_started`。

### 5.5.2 `background_task_unresolved`

若 completion 耗尽归属重试后仍无法安全解析 `delegation_id → origin_turn_key`，服务端不得绑定到最新 user turn，也不得启动 wakeup。它必须发送一个可观察的失败终态，使订阅方不会无限等待：

```text
event: background_task_unresolved
id: deleg_123:unresolved:17
data: {
  "schema_version": 1,
  "event_id": "deleg_123:unresolved:17",
  "event_type": "background_task_unresolved",
  "session_id": "session_123",
  "emitted_at": 1786004422,
  "background_activity_version": 17,
  "payload": {
    "delegation_id": "deleg_123",
    "reason": "origin_unresolved",
    "retryable": false
  }
}
```

订阅方将对应本地批次标为失败并等待 `background_tasks_idle`，不能自行创建 wakeup。服务端必须把该批次从未结算集合移除；若该 session 已无其他未结算工作，随后发送 `background_tasks_idle`。

### 5.6 `bg_task_complete`

表示服务端已接受 completion 并准备交给 wakeup 处理。它不保证 wakeup 已启动：忙碌 session 的事件可携带 `wakeup_state=queued`；只有 `server_turn_started` 才证明已创建 wakeup stream。它用于 toast 和侧栏提示，不用于决定会话事件流何时关闭。

```text
event: bg_task_complete
id: deleg_123:complete:14
data: {
  "schema_version": 1,
  "event_id": "deleg_123:complete:14",
  "event_type": "bg_task_complete",
  "session_id": "session_123",
  "emitted_at": 1786004411,
  "background_activity_version": 14,
  "payload": {
    "delegation_id": "deleg_123",
    "child_task_count": 2,
    "goals": ["调研模型 A", "调研模型 B"],
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "running",
    "child_task_summary": {
      "total": 2,
      "completed": 1,
      "failed": 1,
      "cancelled": 0
    }
  }
}
```

订阅方以 `event_id` 去重。该事件不创建 wakeup；仍有其他未结算任务时必须继续保持 SSE，服务端会在聚合空闲时发送 `background_tasks_idle`。

### 5.7 `server_turn_started`

表示服务端已为异步完成启动一个新的 Agent run。订阅方不得再次 `POST /api/chat/start`；而是直接用响应里的 `stream_id` 接入正常聊天流。

```text
event: server_turn_started
id: deleg_123:run-started:15
data: {
  "schema_version": 1,
  "event_id": "deleg_123:run-started:15",
  "event_type": "server_turn_started",
  "session_id": "session_123",
  "emitted_at": 1786004412,
  "background_activity_version": 15,
  "payload": {
    "delegation_id": "deleg_123",
    "child_task_count": 2,
    "goals": ["调研模型 A", "调研模型 B"],
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "running",
    "child_task_summary": {
      "total": 2,
      "completed": 1,
      "failed": 1,
      "cancelled": 0
    },
    "stream_id": "stream_wakeup_123",
    "source": "async_delegation_wakeup"
  }
}
```

订阅方需要消费该 wakeup 回复时：

```text
GET /api/chat/stream?stream_id=stream_wakeup_123
```

订阅方也可以不附着该活跃 stream，改为在完成后读取持久化 session 结果。

### 5.8 `background_tasks_idle`

该事件由服务端基于 session sidecar 聚合得出，是连接未中断时订阅方关闭按需会话事件流的聚合终态信号。页面刷新或重连后的首帧关闭判断由空 `background_tasks_snapshot` 承担，因为此前的 idle 可能已经错过。

```text
event: background_tasks_idle
id: background:idle:16
data: {
  "schema_version": 1,
  "event_id": "background:idle:16",
  "event_type": "background_tasks_idle",
  "session_id": "session_123",
  "emitted_at": 1786004420,
  "background_activity_version": 16,
  "payload": {
    "active_task_count": 0,
    "active_delegation_count": 0,
    "active_child_task_count": 0,
    "settled_at": 1786004420
  }
}
```

| `payload` 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `active_task_count` | integer | 是 | 兼容字段，必须为 `0`。 |
| `active_delegation_count` | integer | 是 | 必须为 `0`。 |
| `active_child_task_count` | integer | 是 | 必须为 `0`。 |
| `settled_at` | number | 是 | 服务端完成聚合判断的 Unix 时间戳。 |

服务端只在更新 sidecar 后判断所有 delegation 记录。仅当不存在 `status=running`，且不存在 `wakeup_state` 为 `idle`、`queued` 或 `running` 的记录时，才能发送该事件。

### 5.9 实施验收条件

回归测试至少证明以下行为：

| 场景 | 必须证明的结果 |
| --- | --- |
| 派发通知 | 后端在 sidecar 成功写入后向当前 chat stream 发 `background_task_dispatched`；订阅方使用事件顶层 `session_id` 生成固定路径，建立或复用 session SSE。 |
| chat SSE 重连 | 父 turn 结束前重连时，服务端重放尚未确认的 `background_task_dispatched`；订阅方不解析 `tool_complete`，并以首个 `background_tasks_snapshot` 校正状态。 |
| 刷新后恢复 | 客户端持久化待跟踪 session ID，重新订阅后服务端必发首个 `background_tasks_snapshot`；非空快照恢复追踪，空快照关闭连接并移除该 ID。 |
| 统一信封 | 六类会话后台事件的 SSE `event:` / `id:` 与 JSON `event_type` / `event_id` 一致；公共字段齐全，事件专属字段只出现在 `payload`。chat stream 的 `background_task_dispatched` 使用相同 JSON 信封，但其 SSE `id:` 保留聊天 run-journal 游标。 |
| run 间空档完成 | 没有活跃 `STREAMS` 时，订阅方仍从按会话 SSE 收到 `background_task_status`、`bg_task_complete` 和 `server_turn_started`。 |
| 当前会话忙碌 | completion 先报告 `wakeup_state=queued`；当前 run 终态后才启动下一条 wakeup stream。 |
| 多会话 | session A 的事件不会写入 session B 的 SSE 连接或其订阅状态。 |
| 重连 | 断线期间 wakeup 仍执行；重连后，活跃 run 可重新附着，已结束 run 用 snapshot/持久化 session 恢复。 |
| 批次语义 | 一个含多个 child task 的 delegation 只产生一条 completion、一条 `server_turn_started` 和一条 wakeup stream；`child_task_summary` 正确汇总子结果。 |
| 生命周期释放 | 每条 HTTP SSE 在 disconnect、错误、替换和正常关闭时均取消 `SessionChannel` 订阅；最后一个 delegation 批次结算后才发送 `background_tasks_idle`。 |
| 幂等 | 同一个 completion 的重复投递不会重复启动 wakeup，也不会让订阅方重复处理同一 `event_id`。 |
| 会话级取消 | `cancelling` 期间的重复调用不扩大取消范围；每个已覆盖批次收口为真实终态；晚到 completion 不产生 wakeup。 |
| 取消完成查询 | 取消 `POST` 返回 `202` 后只轮询对应 session 的取消 `GET`；`state=settled` 才表示该次固定范围已收口，不能用 `background_tasks_idle` 替代。 |
| 每轮状态 | 状态写入 origin user message 的 `async_delegations`；全部取消为 `cancelled`，其他全量结算结果为 `settled`。 |
| 归属缺失 | 重试耗尽后发送 `background_task_unresolved`，不创建 wakeup；最终仍发 `background_tasks_idle`，订阅方不会无限等待。 |

## 6. 订阅方最小处理契约

本文不规定订阅方的 UI、状态容器或页面切换逻辑。任意 SSE 消费者只需满足以下协议行为：

```text
background_task_dispatched
  → 使用 session_id 建立或复用 /api/sessions/{session_id}/events

background_tasks_snapshot
  → 用 payload.tasks 作为该 session 未结算 delegation 批次的完整基线
  → 两个活动计数都为 0 时，关闭刚建立的连接并删除持久化待跟踪 session ID

background_task_status / bg_task_complete
  → 按 event_id 去重，并按 background_activity_version 应用增量

background_task_unresolved
  → 将 delegation_id 标记为失败；不创建 wakeup，继续等待 background_tasks_idle

server_turn_started
  → 可选地使用 stream_id 附着正常 chat stream；不得再次 POST /api/chat/start

background_tasks_idle(active_delegation_count=0, active_child_task_count=0)
  → 未刷新时，仅当 session_id 与连接一致、event_id 尚未处理且 version 不早于已知 version 时，关闭该 session 的 SSE 连接
```

`background_tasks_idle` 不是任意终态的关闭快捷方式。订阅方还必须验证其 `active_delegation_count` 与 `active_child_task_count` 都为 `0`；旧版本的 idle 到达时必须忽略。页面刷新后的首个关闭判断应以无条件 `background_tasks_snapshot` 为准，因为刷新前的 idle 可能已经错过。之后父 chat stream 有新的 `background_task_dispatched` 时，订阅方重新建立该 session 的会话 SSE。

订阅方可以基于自身业务决定如何展示、缓存或分发事件；这些行为不属于本接口的契约。

## 7. 并发、失败与恢复

### 多任务

同一 `origin_turn_key` 可以有多个 delegation 批次，每个批次有自己的 `delegation_id`；一个批次内部的多个 child task 共享该 ID。每个批次独立走 `running → completed/failed/cancelled` 与 `idle → queued/running → settled`，但只有最后一个批次及其 wakeup 结算后才发 `background_tasks_idle`。

一次父聊天 turn 中的多个派发示例：

```text
父 chat stream
  → background_task_dispatched(deleg_1)
  → background_task_dispatched(deleg_2)

同一条 session SSE
  → background_task_status(deleg_1)
  → bg_task_complete(deleg_2)
  → server_turn_started(deleg_1, stream_wakeup_1)
  → background_task_status(deleg_2, wakeup_state=queued)
  → server_turn_started(deleg_2, stream_wakeup_2)
  → background_tasks_idle
```

`background_task_dispatched` 的数量取决于父 turn 实际调用 `delegate_task` 的次数；它们只在父 chat stream 中出现，一个调用内部包含多少 child task 不会增加该事件数量。`server_turn_started` 同样按 delegation 批次
产生，但同一 session 的 Agent wakeup 按串行规则启动：前一个 wakeup 仍在运行时，后续批次
保持 `wakeup_state=queued`，不会并行开启第二个 session Agent turn。不同的 `stream_id`
仍分别使用 `GET /api/chat/stream?stream_id=...` 接收对应 wakeup 的 token。

### 忙碌会话

后台完成时如 session 已有活跃 Agent run，服务端设置 `wakeup_state=queued`，等待当前 run 的 `done`、`error` 或 `cancel` 终态后再启动 wakeup。不得在同一 session 并行运行两个 Agent stream。

### 归属缺失

服务端只能由 `delegation_id → origin_turn_key` sidecar 映射恢复归属。映射缺失时短暂重试；耗尽后 fail closed：不绑定到“最新 user 消息”、不创建猜测性的 wakeup run，并发送 `background_task_unresolved`。

### 断线与恢复

后台任务不会因为 SSE 连接断开而取消。重新订阅时，若 wakeup 尚在运行，订阅方可恢复其 `stream_id`；若其已结束，可依据 session snapshot/持久化消息恢复状态。订阅方不得假设每个实时事件都必然送达。

### 取消与新派发

会话级取消只覆盖该次请求在 session agent lock 内快照到的 delegation。取消完成前后，新的
user turn 仍可派发新的 delegation；它们不属于正在进行的取消范围，也不能被其取消屏障拦截。
取消收口后的下一次调用则会成为新的取消操作。服务端通过
`delegation_id → origin_turn_key` 索引定位对应 user message，不能扫描消息正文猜测归属。
客户端在 `POST` 返回 `202` 后轮询取消 `GET`，直到读到 `state=settled`；不要重试 `POST` 来查询状态，也不要以 session SSE 的 idle 事件代替该判断。

## 8. 实现责任

| 责任方 | 责任 |
| --- | --- |
| Hermes Agent | 创建后台委派、执行子任务、产出 completion。 |
| WebUI streaming callback | 在 `delegate_task` 成功后保存 `delegation_id → origin_turn_key`，再向当前 chat stream 发 `background_task_dispatched`。 |
| WebUI background processor | 处理 completion、更新 sidecar、单 session 排队、服务端启动 wakeup。 |
| WebUI session-event producer | 推送任务状态、`server_turn_started`、`background_task_unresolved`，并在聚合终态时推送 `background_tasks_idle`；新订阅建立后发送 `background_tasks_snapshot`。 |
| SSE 订阅方 | 仅在需要时订阅事件流；按 session 隔离状态；可用 `stream_id` 接入 wakeup 聊天流；在 `background_tasks_idle` 后释放连接。 |

相关实现/契约：

- [`api/streaming.py`](../../api/streaming.py)：记录 `delegate_task` 派发归属，结算 wakeup 状态。
- [`api/background_process.py`](../../api/background_process.py)：处理异步完成和会话级状态 fan-out。
- [`api/routes.py`](../../api/routes.py)：普通聊天启动、聊天 SSE 与 server-side turn 启动通知。
- [`async-delegation-turn-alignment.md`](../rfcs/async-delegation-turn-alignment.md)：异步委派的 turn 归属与单流不变量。
