# 异步子任务的会话事件接口交互

- **状态：** 提案
- **范围：** WebUI 中 `delegate_task(mode="background")` 的浏览器侧订阅、后台完成、wakeup 回复与多会话资源释放。

本文定义目标接口交互，不改变 Hermes Agent 的 `delegate_task` 工具、异步委派数据库或 completion 事件格式。

> `background_tasks_idle` 是本文提出的聚合终态事件；当前代码不应把它当作已存在的 SSE 事件。

## 当前实现状态与缺口

当前实现中，`background_task_status`、`bg_task_complete`、`server_turn_started` 已有**后端发射点**；下表同时列出本文新增的两个建连事件：

| 事件 | 发射时机 | 当前发射目标 |
| --- | --- | --- |
| `background_task_dispatched` | **提案**：成功记录派发归属后 | 必须发往派发它的当前 chat stream；已订阅的 session 同时经 `SessionChannel` 接收。 |
| `background_tasks_snapshot` | **提案**：session SSE 建连并完成 sidecar 读取后 | 仅发往新建立的 session SSE，用于填补建连竞态。 |
| `background_task_status` | completion 进入 `queued` / `running`，或 wakeup 进入 `settled` / `failed` | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `bg_task_complete` | 服务端已成功接受 completion 并启动 wakeup | 匹配的活跃聊天流，以及 `SessionChannel`。 |
| `server_turn_started` | 服务端成功创建 wakeup run | `SessionChannel`。 |

但 `GET /api/sessions/{session_id}/events` 当前接入的是 run-journal 回放、活跃 run 订阅和 snapshot 恢复路径，**没有订阅 `SessionChannel`**。因此不能把上述三个事件描述为该 HTTP SSE 接口已经保证交付的事件；特别是在两个 Agent run 之间没有活跃聊天流时，事件不能通过该接口抵达浏览器。

本文其余章节描述要补齐的接口契约。实施时应在同一个路径中保留现有 journal 回放/`session_snapshot` 行为，并额外桥接 `SessionChannel` 生命周期事件；不得另起第二条浏览器 transport，也不得把 assistant token 转移到会话事件流。现有生命周期事件的平铺 payload 应迁移为第 5.2 节定义的统一事件信封，前端不应长期兼容两种数据形状。

## 1. 目标与边界

一个会话可以派发多个后台子任务，用户也可以切换到其他会话继续对话。目标是：

- 只有含未结算后台工作的会话才保持会话级 SSE；
- 子任务完成后，服务端而不是浏览器启动 parent wakeup turn；
- wakeup 回复仍通过独立的聊天流发送，绝不把 token 混入会话事件流；
- 一个会话同一时刻只运行一个 Agent turn；
- 多个会话可各自持有后台任务订阅；切换聊天页不应使其他会话丢失完成通知；
- 最后一个后台任务及其 wakeup 都结算后，服务端发出“可关闭”的聚合事件。

这里的“后台任务结算”包含两层：

| 层 | 字段 | 完成条件 |
| --- | --- | --- |
| 子任务执行 | `status` | `completed`、`failed` 或 `cancelled` |
| 父会话处理 | `wakeup_state` | `settled` 或 `failed` |

仅 `status=completed` 不能释放会话事件流：该子任务的 wakeup 可能仍在排队或生成回复。

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
    participant UI as Browser
    participant Chat as /api/chat/start + chat stream
    participant Agent as Hermes Agent
    participant Sidecar as WebUI session sidecar
    participant Events as /api/sessions/{id}/events

    UI->>Chat: POST /api/chat/start
    Chat-->>UI: { stream_id: stream_1 }
    UI->>Chat: GET /api/chat/stream?stream_id=stream_1
    Chat->>Agent: run parent turn
    Agent-->>Chat: tool_complete(delegate_task, dispatched)
    Chat->>Sidecar: delegation_id -> origin_turn_key; status=running
    Chat-->>UI: background_task_dispatched + session_events_url
    UI->>Events: GET session_events_url
    Events-->>UI: background_tasks_snapshot（sidecar 当前状态）
    Chat-->>UI: done / stream_end (原父 turn)

    Agent-->>Sidecar: async_delegation completion
    Sidecar-->>Events: background_task_status
    alt session 空闲
        Sidecar->>Chat: 服务端启动 async_delegation_wakeup
    else session 有活跃 turn
        Sidecar-->>Events: wakeup_state=queued
        Note over Sidecar,Chat: 当前 turn 终态后按完成顺序启动 wakeup
        Sidecar->>Chat: 服务端启动 async_delegation_wakeup
    end
    Chat-->>Events: server_turn_started { stream_id: stream_2 }
    UI->>Chat: GET /api/chat/stream?stream_id=stream_2
    Chat-->>UI: token / tool / done / stream_end（后台回复）
    Sidecar-->>Events: background_task_status wakeup_state=settled
    opt 所有后台任务均已结算
        Sidecar-->>Events: background_tasks_idle
        UI->>Events: EventSource.close()
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

浏览器随后打开：

```text
GET /api/chat/stream?stream_id=stream_parent_1
```

### 4.2 派发通知与会话 SSE 建连

当前前端可在正常聊天流的 `tool_complete` 中识别 `delegate_task` 的成功结果：

```json
{
  "status": "dispatched",
  "mode": "background",
  "delegation_id": "deleg_123"
}
```

目标实现中，后端在校验该工具结果并成功写入 `delegation_id → origin_turn_key` sidecar 映射后，必须向**同一条仍处于连接状态的 chat SSE**发出专用 `background_task_dispatched` 事件。前端以该事件提供的地址建立会话级事件流：

```text
GET /api/sessions/session_123/events
Accept: text/event-stream
```

这避免前端解析通用工具结果来决定 transport 行为。因为这个通知走的是已连接的 chat SSE，它不会出现“必须先订阅 session SSE 才能收到建立 session SSE 的通知”的循环。

同一 session 的多个后台任务共用一条 EventSource。前端不能每个 `delegation_id` 建一条连接。

`tool_complete` 保留为兼容/断线兜底：若前端因 chat SSE 重连等原因错过专用事件，但仍拿到成功工具结果，应按相同 `session_id` 主动建立 session SSE。无论通过哪一种路径建立，session SSE 的第一个业务帧必须是 `background_tasks_snapshot`，以 sidecar 的当前状态消除“派发通知已送达、但 session SSE 尚未连上时任务状态发生变化”的竞态。

## 5. 会话事件流

### 5.1 连接与通用规则

```http
GET /api/sessions/{session_id}/events
Accept: text/event-stream
Last-Event-ID: <可选，最后已处理的 event id>
```

- EventSource 以 `session_id` 为 key 管理；一个 session 最多一条订阅。
- 事件只更新其所属 session 的后台状态。非当前会话只更新侧栏 badge/未读状态，不渲染聊天正文。
- 重连后客户端按事件 ID 去重；无法安全回放时，服务端发送 session snapshot，客户端用 `GET /api/session` 或 snapshot 重建本地状态。
- 即使浏览器没有订阅，后台任务和 server-side wakeup 仍必须继续执行；SSE 仅是观察通道。

### 5.2 统一事件信封（新增提案）

所有后台任务生命周期及其建连快照事件都使用同一 JSON 顶层形状。SSE 的 `event:` 行与 `event_type` 值相同，SSE 的 `id:` 与 JSON 中的 `event_id` 值相同。

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
| `event_id` | string | 对客户端不透明的唯一事件标识；用于 SSE 重连和去重。 |
| `event_type` | string | 与 SSE `event:` 完全一致。 |
| `session_id` | string | 事件所属 WebUI 会话。 |
| `emitted_at` | number | 服务端发射时的 Unix 时间戳。 |
| `background_activity_version` | integer | session sidecar 中单调递增的后台活动版本；客户端忽略早于本地已处理版本的状态。 |
| `payload` | object | 仅包含该事件专属字段。 |

`delegation_id`、`origin_turn_key`、`stream_id`、`status`、`wakeup_state` 和 `active_task_count` 不放在公共顶层。它们只在语义相关的事件 `payload` 中出现，避免空字段被误解为真实状态。

### 5.2.1 待补齐的服务端桥接

`GET /api/sessions/{session_id}/events` 的 handler 应同时拥有两类订阅：

```text
run journal / active StreamChannel
  → 保留既有恢复/回放、run journal cursor、session_snapshot；实时 assistant token 仍以 chat stream 为准

SessionChannel(session_id)
  → background_task_dispatched（已订阅的其他标签页）
  → background_task_status
  → bg_task_complete
  → server_turn_started
  → background_tasks_idle（实施后）
```

桥接要求：

1. 先完成 session 可见性与认证校验，再以 `session_id` 原子订阅 `SessionChannel`。
2. 从订阅成功到 HTTP SSE 头、初始恢复和循环写入的所有路径，都必须被同一个 `try/finally` 覆盖；每次退出均执行 `SessionChannel.unsubscribe()`。
3. journal replay 继续使用既有 `Last-Event-ID` / snapshot 语义；`SessionChannel` 的瞬时生命周期事件不伪造为 run-journal token，也不能假称已被 journal 精确重放。
4. 同一事件可能在活跃聊天流和 `SessionChannel` 两条内部路径同时可见；客户端只以统一 `event_id` 去重，不能重复创建 toast、badge 或 chat renderer。
5. `server_turn_started` 只通知浏览器附着既有 `stream_id`，浏览器不得据此再次调用 `POST /api/chat/start`。

### 5.3 `background_task_dispatched`（新增提案）

该事件只表示后台委派已被后端接受、归属已持久化；它通知派发该任务的浏览器建立或复用 session SSE，不是任务实际完成的信号。

服务端发射顺序必须为：

```text
delegate_task 返回 dispatched
  → 校验 delegation_id
  → 持久化 delegation_id → origin_turn_key sidecar 映射
  → 成功后向当前 chat stream 发 background_task_dispatched
  → 同时向已订阅的 SessionChannel 尽力广播
```

sidecar 写入失败时不得发事件。这样浏览器不会显示一个服务端无法定位、恢复或结算的后台任务。

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
    "origin_turn_key": "turn:8",
    "status": "running",
    "wakeup_state": "idle",
    "dispatched_at": 1786004400,
    "session_events_url": "/api/sessions/session_123/events"
  }
}
```

发往当前 chat stream 的副本是首次建连的首选通知；`session_events_url` 是相对 WebUI 根路径，前端应通过 `new URL(value, document.baseURI)` 解析，而不是拼接 host。向 `SessionChannel` 的副本是尽力通知，供已订阅的其他标签页更新侧栏 badge。

若当前 chat stream 已断开，或前端在迁移期只收到旧 `tool_complete`，前端仍可从工具结果的 `session_id` / `delegation_id` 建立默认的 `/api/sessions/{session_id}/events` 连接。连接成功后的 `background_tasks_snapshot` 是最终状态依据。

### 5.4 `background_tasks_snapshot`（新增提案）

每次 session SSE 成功完成认证、可见性校验和 `SessionChannel` 订阅后，服务端立即从该 session 的 sidecar 构建并只向这个新订阅者发送一次快照。它解决以下竞态：

```text
chat stream 收到 background_task_dispatched
  → 前端开始建立 session SSE
  → 子任务在 HTTP 建连期间完成或进入 queued
  → snapshot 返回当前记录，前端不依赖是否错过中间事件
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
    "tasks": [
      {
        "delegation_id": "deleg_123",
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

`tasks` 只包含未结算任务，即 `status=running` 或 `wakeup_state` 为 `idle`、`queued`、`running` 的记录。前端以快照完整替换该 session 的后台任务本地集合；后续只接受 `background_activity_version` 不早于该快照的增量事件。

### 5.5 `background_task_status`

表示某个 `delegation_id` 的持久化生命周期变化，不创建新的聊天气泡。

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
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "queued"
  }
}
```

`payload` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `delegation_id` | string | 是 | 后台子任务唯一标识。 |
| `origin_turn_key` | string | 是 | 成功派发该任务的真实 user turn。 |
| `status` | string | 是 | `running`、`completed`、`failed`、`cancelled`。 |
| `wakeup_state` | string | 是 | `idle`、`queued`、`running`、`settled`、`failed`。 |

收到 `status=completed, wakeup_state=queued` 时，浏览器必须继续保留该 session 的 EventSource；这不是可关闭状态。

### 5.6 `bg_task_complete`

表示服务端已接受完成事件并准备/正在交给 wakeup 处理。它用于 toast、侧栏提示和兼容性消费，不用于决定会话事件流何时关闭。

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
    "origin_turn_key": "turn:8",
    "status": "completed",
    "wakeup_state": "running"
  }
}
```

客户端以 `event_id` 去重。不能在收到这个事件后关闭 EventSource，因为 wakeup 可能尚未启动、可能处于队列中，或仍未结束。

### 5.7 `server_turn_started`

表示服务端已为异步完成启动一个新的 Agent run。浏览器不得再次 `POST /api/chat/start`；而是直接用响应里的 `stream_id` 接入正常聊天流。

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
    "stream_id": "stream_wakeup_123",
    "source": "async_delegation_wakeup",
    "delegation_id": "deleg_123",
    "origin_turn_key": "turn:8"
  }
}
```

当前聊天页正显示 `session_123` 时：

```text
GET /api/chat/stream?stream_id=stream_wakeup_123
```

如果这不是当前聊天页，前端只记录该 session 有活跃 run；用户切回时再接入仍活跃的 stream，或者读取已经持久化的完成消息。

### 5.8 `background_tasks_idle`（新增提案）

该事件由服务端基于 session sidecar 聚合得出，是客户端关闭按需会话事件流的唯一正向信号。

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
    "settled_at": 1786004420
  }
}
```

| `payload` 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `active_task_count` | integer | 是 | 必须为 `0`。 |
| `settled_at` | number | 是 | 服务端完成聚合判断的 Unix 时间戳。 |

服务端只在更新 sidecar 后判断所有记录。仅当不存在 `status=running`，且不存在 `wakeup_state` 为 `idle`、`queued` 或 `running` 的记录时，才能发送该事件。

### 5.9 实施验收条件

实现 `SessionChannel → /api/sessions/{session_id}/events` 桥接时，至少证明以下行为：

| 场景 | 必须证明的结果 |
| --- | --- |
| 派发通知 | 后端在 sidecar 成功写入后向当前 chat stream 发 `background_task_dispatched`；前端使用其中的 `session_events_url` 建立或复用 session SSE。 |
| 派发事件早到或丢失 | 前端即使错过专用派发事件，仍能用兼容 `tool_complete` 建立会话 SSE；首个 `background_tasks_snapshot` 必须反映当前 sidecar 状态。 |
| 统一信封 | 六类后台事件的 SSE `event:` / `id:` 与 JSON `event_type` / `event_id` 一致；公共字段齐全，事件专属字段只出现在 `payload`。 |
| run 间空档完成 | 没有活跃 `STREAMS` 时，浏览器仍从按会话 SSE 收到 `background_task_status`、`bg_task_complete` 和 `server_turn_started`。 |
| 当前会话忙碌 | completion 先报告 `wakeup_state=queued`；当前 run 终态后才启动下一条 wakeup stream。 |
| 多会话 | session A 的事件不会写入 session B 的 EventSource 或聊天正文。 |
| 重连 | 断线期间 wakeup 仍执行；重连后，活跃 run 可重新附着，已结束 run 用 snapshot/持久化 session 恢复。 |
| 生命周期释放 | 每条 HTTP SSE 在 disconnect、错误、替换和正常关闭时均取消 `SessionChannel` 订阅；最后一个后台任务结算后才发送 `background_tasks_idle`。 |
| 幂等 | 同一个 completion 的重复投递不会重复启动 wakeup，也不会使前端重复渲染或计数。 |

## 6. 客户端状态机与关闭条件

前端维护：

```text
backgroundSessions: Map<session_id, {
  eventSource,
  delegationIds: Set<delegation_id>,
  backgroundActivityVersion
}>
```

状态转换：

```text
background_task_dispatched
  → ensureSessionEvents(session_id)

兼容 tool_complete（专用事件未收到）
  → ensureSessionEvents(session_id)

background_tasks_snapshot
  → 以 payload.tasks 完整替换该 session 的后台任务本地集合

background_task_status / bg_task_complete
  → 更新该 session 的任务状态和侧栏

server_turn_started
  → 当前会话：attachLiveStream(stream_id)
  → 非当前会话：记录 active stream，不渲染 token

background_tasks_idle(active_task_count=0)
  → 仅当 event 的 version 不早于本地已知 version
  → closeSessionEvents(session_id)
```

切换聊天页时：

- 不关闭 `backgroundSessions` 中的 EventSource；
- 只关闭没有后台任务的普通会话订阅（若该 UI 另有此类订阅）；
- 切回目标 session 后，对正在运行的 wakeup 连接其 chat stream；已结算则加载持久化消息。

## 7. 并发、失败与恢复

### 多任务

同一 `origin_turn_key` 可以有多个 `delegation_id`。每个任务独立走 `running → completed` 与 `idle → queued/running → settled`，但只有最后一个任务结算后才发 `background_tasks_idle`。

### 忙碌会话

后台完成时如 session 已有活跃 Agent run，服务端设置 `wakeup_state=queued`，等待当前 run 的 `done`、`error` 或 `cancel` 终态后再启动 wakeup。不得在同一 session 并行运行两个 Agent stream。

### 归属缺失

服务端只能由 `delegation_id → origin_turn_key` sidecar 映射恢复归属。映射缺失时短暂重试；耗尽后 fail closed：不绑定到“最新 user 消息”、不创建猜测性的 wakeup run，并记录 `async_delegation_origin_unresolved`。

### 断线或页面不在前台

后台任务不会因为 EventSource 断开而取消。重新订阅时，若 wakeup 尚在运行，客户端恢复其 `stream_id`；若其已结束，客户端依据 session snapshot/持久化消息恢复显示。客户端不得假设每个实时事件都必然送达。

## 8. 实现责任

| 责任方 | 责任 |
| --- | --- |
| Hermes Agent | 创建后台委派、执行子任务、产出 completion。 |
| WebUI streaming callback | 在 `delegate_task` 成功后保存 `delegation_id → origin_turn_key`，再向当前 chat stream 发 `background_task_dispatched`。 |
| WebUI background processor | 处理 completion、更新 sidecar、单 session 排队、服务端启动 wakeup。 |
| WebUI session-event producer | 推送任务状态、`server_turn_started`，并在聚合终态时推送 `background_tasks_idle`；新订阅建立后发送 `background_tasks_snapshot`。 |
| 浏览器 | 仅在需要时订阅事件流；按 session 隔离状态；用 `stream_id` 接入 wakeup 聊天流；在 `background_tasks_idle` 后释放连接。 |

相关实现/契约：

- [`api/streaming.py`](../../api/streaming.py)：记录 `delegate_task` 派发归属，结算 wakeup 状态。
- [`api/background_process.py`](../../api/background_process.py)：处理异步完成和会话级状态 fan-out。
- [`api/routes.py`](../../api/routes.py)：普通聊天启动、聊天 SSE 与 server-side turn 启动通知。
- [`static/messages.js`](../../static/messages.js)：EventSource 与 wakeup `stream_id` 的前端接入点。
- [`async-delegation-turn-alignment.md`](../rfcs/async-delegation-turn-alignment.md)：异步委派的 turn 归属与单流不变量。
