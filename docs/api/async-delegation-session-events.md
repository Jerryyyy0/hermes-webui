# 异步委派批次的会话事件接口交互

- **状态：** 部分已实施；页面刷新恢复所需的无条件快照待服务端实现
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

异步委派生命周期事件由 sidecar 的持久化 `async_delegation_activity_version` 编号。每个状态变更先持久化，再构造统一事件信封并发射：

| 事件 | 发射时机 | 当前发射目标 |
| --- | --- | --- |
| `background_task_dispatched` | 成功记录派发归属后 | 派发它的当前 chat stream；已订阅的 session 同时经 `SessionChannel` 接收。 |
| `background_tasks_snapshot` | 每次 session SSE 建连、完成 sidecar 读取后 | 仅发往新建立的 session SSE；无论是否存在未结算任务都发送，用于填补建连竞态和页面刷新恢复。 |
| `background_task_status` | completion 进入 `queued` / `running`，或 wakeup 进入 `settled` / `failed` | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `bg_task_complete` | 服务端已成功接受 completion 并启动 wakeup | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `server_turn_started` | 服务端成功创建 wakeup run | `SessionChannel`。 |
| `background_tasks_idle` | sidecar 中最后一个任务及其 wakeup 结算后 | 匹配的活跃聊天流，以及 `SessionChannel`。 |

`GET /api/sessions/{session_id}/events` 同时保留 run-journal 回放、活跃 run 订阅和 snapshot 恢复，并原子订阅 `SessionChannel`。含未结算后台工作的连接会跨 Agent run 继续接收生命周期事件；普通会话保留原有的 run 终态关闭行为。

> 当前实现限制：现有 handler 只在 snapshot 的 `active_task_count > 0` 时发送 `background_tasks_snapshot`。因此，若页面刷新期间任务已结算，重新订阅的客户端尚不能从空快照得知可以关闭连接。本文件规定的“每次建连必发快照”是待实现的接口契约。

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

`GET /api/sessions/events` 是全局 session-list invalidation 流，不能替代按会话的 `GET /api/sessions/{session_id}/events`。

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

所有按会话 SSE 的后台任务生命周期及建连快照事件都使用同一 JSON 顶层形状。SSE 的 `event:` 行与 `event_type` 值相同，SSE 的 `id:` 与 JSON 中的 `event_id` 值相同。`background_task_dispatched` 在普通 chat stream 上仍使用该 stream 的 run-journal cursor 作为 SSE `id:`，其 JSON `event_id` 仍用于生命周期去重。

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

delegation 生命周期事件（`background_task_dispatched`、`background_task_status`、`bg_task_complete`、`server_turn_started`）的 `payload` 必须共同包含以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `delegation_id` | string | 后台委派批次唯一标识；批次内的 child task 共享此 ID。 |
| `delegation_kind` | string | `single` 或 `batch`。 |
| `child_task_count` | integer | 该批次启动的 child task 数量。 |
| `goals` | string[] | child task 的目标列表；可能为空。 |
| `origin_turn_key` | string | 派发该 delegation 的真实 user turn。 |
| `status` | string | delegation 批次执行状态：`running`、`completed`、`failed` 或 `cancelled`。 |
| `wakeup_state` | string | 父会话处理状态：`idle`、`queued`、`running`、`settled` 或 `failed`。 |
| `child_task_summary` | object，可选 | completion 后的逐项结果计数；不包含 child 结果正文。 |

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
  → background_task_dispatched（其他已订阅连接）
  → background_task_status
  → bg_task_complete
  → server_turn_started
  → background_tasks_idle
```

桥接要求：

1. 先完成 session 可见性与认证校验，再以 `session_id` 原子订阅 `SessionChannel`。
2. 从订阅成功到 HTTP SSE 头、初始恢复和循环写入的所有路径，都必须被同一个 `try/finally` 覆盖；每次退出均执行 `SessionChannel.unsubscribe()`。
3. journal replay 继续使用既有 `Last-Event-ID` / snapshot 语义；`SessionChannel` 的瞬时生命周期事件不伪造为 run-journal token，也不能假称已被 journal 精确重放。
4. 同一事件可能在活跃聊天流和 `SessionChannel` 两条内部路径同时可见；订阅方只以统一 `event_id` 去重，不能重复执行可观察副作用。
5. `server_turn_started` 只通知订阅方附着既有 `stream_id`；订阅方不得据此再次调用 `POST /api/chat/start`。

### 5.3 `background_task_dispatched`

该事件只表示后台委派已被后端接受、归属已持久化；它通知派发该任务的订阅方建立或复用 session SSE，不是任务实际完成的信号。

服务端发射顺序必须为：

```text
delegate_task 返回 dispatched
  → 校验 delegation_id
  → 持久化 delegation_id → origin_turn_key sidecar 映射
  → 成功后向当前 chat stream 发 background_task_dispatched
  → 同时向已订阅的 SessionChannel 尽力广播
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
    "delegation_kind": "batch",
    "child_task_count": 2,
    "goals": ["调研模型 A", "调研模型 B"],
    "origin_turn_key": "turn:8",
    "status": "running",
    "wakeup_state": "idle",
    "dispatched_at": 1786004400
  }
}
```

发往当前 chat stream 的副本是首次建连的首选通知。订阅方以顶层 `session_id` 生成 `/api/sessions/{session_id}/events`，无需后端下发 URL。向 `SessionChannel` 的副本是尽力通知，供其他已订阅连接消费。

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
        "delegation_kind": "batch",
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
    "delegation_kind": "batch",
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
| `delegation_kind` | string | 是 | `single` 或 `batch`。 |
| `child_task_count` | integer | 是 | 批次中的 child task 数量。 |
| `goals` | string[] | 是 | 批次中各 child task 的目标；没有目标时为空数组。 |
| `origin_turn_key` | string | 是 | 成功派发该 delegation 的真实 user turn。 |
| `status` | string | 是 | `running`、`completed`、`failed`、`cancelled`。 |
| `wakeup_state` | string | 是 | `idle`、`queued`、`running`、`settled`、`failed`。 |
| `child_task_summary` | object | 否 | completion 后的 child 结果计数。 |

收到 `status=completed, wakeup_state=queued` 时，订阅方必须继续保留该 session 的 SSE 连接；这不是可关闭状态。

### 5.6 `bg_task_complete`

表示服务端已接受完成事件并准备/正在交给 wakeup 处理。它用于 toast 和侧栏提示，不用于决定会话事件流何时关闭。

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
    "delegation_kind": "batch",
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

订阅方以 `event_id` 去重。不能在收到这个事件后关闭 SSE 连接，因为 wakeup 可能尚未启动、可能处于队列中，或仍未结束。

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
    "delegation_kind": "batch",
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
| 统一信封 | 六类后台事件的 SSE `event:` / `id:` 与 JSON `event_type` / `event_id` 一致；公共字段齐全，事件专属字段只出现在 `payload`。 |
| run 间空档完成 | 没有活跃 `STREAMS` 时，订阅方仍从按会话 SSE 收到 `background_task_status`、`bg_task_complete` 和 `server_turn_started`。 |
| 当前会话忙碌 | completion 先报告 `wakeup_state=queued`；当前 run 终态后才启动下一条 wakeup stream。 |
| 多会话 | session A 的事件不会写入 session B 的 SSE 连接或其订阅状态。 |
| 重连 | 断线期间 wakeup 仍执行；重连后，活跃 run 可重新附着，已结束 run 用 snapshot/持久化 session 恢复。 |
| 批次语义 | 一个含多个 child task 的 delegation 只产生一条 completion、一条 `server_turn_started` 和一条 wakeup stream；`child_task_summary` 正确汇总子结果。 |
| 生命周期释放 | 每条 HTTP SSE 在 disconnect、错误、替换和正常关闭时均取消 `SessionChannel` 订阅；最后一个 delegation 批次结算后才发送 `background_tasks_idle`。 |
| 幂等 | 同一个 completion 的重复投递不会重复启动 wakeup，也不会让订阅方重复处理同一 `event_id`。 |

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

server_turn_started
  → 可选地使用 stream_id 附着正常 chat stream；不得再次 POST /api/chat/start

background_tasks_idle(active_delegation_count=0, active_child_task_count=0)
  → 未刷新时，仅当 session_id 与连接一致、event_id 尚未处理且 version 不早于已知 version 时，关闭该 session 的 SSE 连接
```

`background_tasks_idle` 不是任意终态的关闭快捷方式。订阅方还必须验证其 `active_delegation_count` 与 `active_child_task_count` 都为 `0`；旧版本的 idle 到达时必须忽略。页面刷新后的首个关闭判断应以无条件 `background_tasks_snapshot` 为准，因为刷新前的 idle 可能已经错过。之后有新的 `background_task_dispatched` 时，订阅方重新建立该 session 的会话 SSE。

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

`background_task_dispatched` 的数量取决于父 turn 实际调用 `delegate_task` 的次数；一个调用
内部包含多少 child task 不会增加该事件数量。`server_turn_started` 同样按 delegation 批次
产生，但同一 session 的 Agent wakeup 按串行规则启动：前一个 wakeup 仍在运行时，后续批次
保持 `wakeup_state=queued`，不会并行开启第二个 session Agent turn。不同的 `stream_id`
仍分别使用 `GET /api/chat/stream?stream_id=...` 接收对应 wakeup 的 token。

### 忙碌会话

后台完成时如 session 已有活跃 Agent run，服务端设置 `wakeup_state=queued`，等待当前 run 的 `done`、`error` 或 `cancel` 终态后再启动 wakeup。不得在同一 session 并行运行两个 Agent stream。

### 归属缺失

服务端只能由 `delegation_id → origin_turn_key` sidecar 映射恢复归属。映射缺失时短暂重试；耗尽后 fail closed：不绑定到“最新 user 消息”、不创建猜测性的 wakeup run，并记录 `async_delegation_origin_unresolved`。

### 断线与恢复

后台任务不会因为 SSE 连接断开而取消。重新订阅时，若 wakeup 尚在运行，订阅方可恢复其 `stream_id`；若其已结束，可依据 session snapshot/持久化消息恢复状态。订阅方不得假设每个实时事件都必然送达。

## 8. 实现责任

| 责任方 | 责任 |
| --- | --- |
| Hermes Agent | 创建后台委派、执行子任务、产出 completion。 |
| WebUI streaming callback | 在 `delegate_task` 成功后保存 `delegation_id → origin_turn_key`，再向当前 chat stream 发 `background_task_dispatched`。 |
| WebUI background processor | 处理 completion、更新 sidecar、单 session 排队、服务端启动 wakeup。 |
| WebUI session-event producer | 推送任务状态、`server_turn_started`，并在聚合终态时推送 `background_tasks_idle`；新订阅建立后发送 `background_tasks_snapshot`。 |
| SSE 订阅方 | 仅在需要时订阅事件流；按 session 隔离状态；可用 `stream_id` 接入 wakeup 聊天流；在 `background_tasks_idle` 后释放连接。 |

相关实现/契约：

- [`api/streaming.py`](../../api/streaming.py)：记录 `delegate_task` 派发归属，结算 wakeup 状态。
- [`api/background_process.py`](../../api/background_process.py)：处理异步完成和会话级状态 fan-out。
- [`api/routes.py`](../../api/routes.py)：普通聊天启动、聊天 SSE 与 server-side turn 启动通知。
- [`async-delegation-turn-alignment.md`](../rfcs/async-delegation-turn-alignment.md)：异步委派的 turn 归属与单流不变量。
