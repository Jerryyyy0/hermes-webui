# 取消后立即发送消息的后端防吞方案

## 结论

“取消旧消息后立即发送新消息”确实可能吞消息。对外置前端而言，关键竞态是：

1. `/api/chat/cancel` 为了快速返回，提前清理 `active_stream_id` 和旁路运行状态；
2. 外置前端紧接着调用 `/api/chat/start`，新 worker 可能在旧 worker 完全退出前启动；
3. 旧 worker 的迟到 `finally`、checkpoint 或取消写回，可能清理或覆盖新 worker 的会话字段。

因此，`/api/chat/cancel` 返回成功并不等于旧 worker 已完全退出，`/api/chat/start` 返回成功也不等于新消息已经有可恢复的持久提交点。

本方案的目标是：

> 后端一旦接受新消息，就先把它写入会话级持久队列；取消、网络断开、worker 迟到退出或服务重启都不能使该队列项静默消失。只有新运行成功创建并获得 `stream_id` 后，队列项才算发送完成。

前端不承担可靠性。本文只覆盖外置前端实际使用的 `/api/chat/cancel` 和 `/api/chat/start`；`/api/chat/steer` 不在本方案调用链内，也不作为问题根因或修复前提。

### 方案可行性结论

该方案落地后可以保证消息不被静默吞掉，但当前代码尚未具备这个保证。当前 `/api/chat/start` 在检测到活动运行时仍返回 409，`cancel_stream()` 也会在 worker 完全退出前释放旁路 owner。必须完成本文列出的队列、取消代次和迟到 worker 防护后，才能宣称“取消后立即发送不会丢消息”。

## cancel 能否彻底杀死 stream

当前实现只能做到“逻辑取消”，不能保证“立刻强杀执行线程”：

- `cancel_stream()` 设置 `CANCEL_FLAGS`、调用 `agent.interrupt()`，并向已建立的 SSE channel 写入 cancel 终态；因此客户端不会再收到正常 token。
- WebUI worker 使用 Python thread 执行。若线程卡在模型 SDK、网络 socket、子进程等待或不可中断的工具调用中，`Event.set()` 和 `interrupt()` 都不能安全地强制杀死该线程。
- `/api/chat/cancel` 最多等待 `CANCEL_SETTLE_TIMEOUT_SECONDS`（当前为 10 秒）；超时只会返回 `settled: false`，不代表 worker 已退出。
- 当前代码会提前移除 `STREAMS`、`CANCEL_FLAGS`、`AGENT_INSTANCES` 和 `active_stream_id`，这是为了让 cancel 快速返回，但也意味着旧 worker 可能仍在 `ACTIVE_RUNS` 中运行。

因此，接口应明确区分三层状态：

```text
stream_closed       # SSE 不再向客户端输出
cancel_requested    # 已发出取消信号
worker_settled      # worker finally 已执行并从 ACTIVE_RUNS 注销
```

### 不引入进程隔离时

把当前 cancel 改成“先标记 `cancelling`，保留旧 owner，等待 `ACTIVE_RUNS` 注销后再清理 owner”。超时返回 `settled: false`，此时 `/api/chat/start` 只能入队，不能启动 successor。这样不能强杀卡死线程，但能保证旧 worker 不会与新 worker 并发写同一 session，也不会吞下一条消息。

同时为模型和工具调用增加可取消的下游句柄（HTTP 请求 abort、子进程 terminate、工具级 cancel hook）。每个调用都必须在取消后释放句柄；没有 abort 能力的调用只能等待超时，不能伪造 `settled: true`。

### 需要真正强杀时

将每个 Agent turn 放到独立 subprocess 或可终止的 worker 容器中。cancel 流程为：

1. 持久化取消状态和下一条消息队列；
2. 关闭 SSE 输出并发送 graceful cancel；
3. 在限定时间内等待 worker 正常退出；
4. 超时后对该 turn 的专属进程执行 terminate/kill；
5. 只在进程退出确认、`ACTIVE_RUNS` 注销和 session owner 代次匹配后，才允许 successor 启动。

进程级强杀会丢失未刷新的内存输出，所以 partial transcript、用户输入和队列必须在强杀前通过 WAL/持久化 checkpoint 保存。不能直接 `kill` WebUI 主进程，也不能尝试从 Python 强行杀线程。

## 代码证据

- `cancel_stream()` 会调用 `agent.interrupt()`，随后提前移除运行状态并清空 `active_stream_id`：[`api/streaming.py`](../api/streaming.py#L11122-L11396)。
- `/api/chat/cancel` 会等待 worker settle，但超时后仍返回，调用方不能把 `settled: false` 当成可立即复用会话的许可：[`api/routes.py`](../api/routes.py#L11543-L11628)。
- `/api/chat/start` 已检查 `active_stream_id`、`ACTIVE_RUNS` 和 pending grace，但这些检查必须与取消状态和 successor owner 使用同一个代次模型：[`api/routes.py`](../api/routes.py#L18702-L18773)、[`api/routes.py`](../api/routes.py#L18812-L18980)。
- WebUI 已有 `_get_session_agent_lock(session_id)`，并规定只保护内存状态和 `save()`，不能跨越 LLM/HTTP 网络调用：[`api/config.py`](../api/config.py#L9158-L9178)。
- `ACTIVE_RUNS` 才是 worker 生命周期的事实来源。取消提前清除旁路 stream 状态时，旧 worker 仍可能存在：[`api/routes.py`](../api/routes.py#L18702-L18770)。

## 后端状态模型

### 会话控制状态

在 Session 增加或等价维护以下字段（可先放在现有 session metadata，稳定后再正式建模）：

```text
control_generation: int                 # 每次新 turn 递增
active_stream_id: str | null            # 当前可接受控制的 stream
active_stream_generation: int | null
cancel_state: idle | cancelling | settled
cancel_stream_id: str | null
cancel_generation: int | null
queued_user_messages: [QueueEntry]
```

`active_stream_id is null` 只能表示“旁路所有权已释放”，不能表示旧 worker 已退出，也不能单独作为新消息是否可发送的依据。`ACTIVE_RUNS` 和 `cancel_state` 必须一起参与判断。

### 后端队列项

普通新消息和 Stop-and-send 消息使用同一种持久结构：

```json
{
  "entry_id": "uuid",
  "session_id": "session-id",
  "text": "完整用户输入",
  "attachments": [],
  "created_at": 1710000000.0,
  "source": "queue|stop_and_send",
  "origin_stream_id": "old-stream-id",
  "origin_generation": 7,
  "status": "queued|dispatching|sent|failed",
  "attempts": 0,
  "last_error": null
}
```

队列项必须与 `session_id`、创建代次和原 stream 绑定。任何异常路径只能把项留在 `queued` 或标成可重试的 `failed`，不能直接删除。

## 必须保持的不变量

1. **先持久化，后取消。** 后端接受 Stop-and-send 或取消窗口内的新消息时，必须在 session lock 内先追加队列并 `save()`，持久化成功后才调用 `interrupt()`。
2. **队列以完整 successor 提交为提交点。** drain 只能 `peek` 队首；只有新用户输入已持久化、successor 的 `stream_id + generation` 已登记、worker 已进入可恢复启动状态后，才把队列项原子标记为 `sent`。任一环节失败都保留队列项并可重试。
3. **旧 worker 不得清理 successor。** worker 的 `finally`、checkpoint 和终态写回必须同时校验 `stream_id + generation`。校验失败时只能清理自己的 `ACTIVE_RUNS` 条目，不能改动 successor 的 `active_stream_id`、`pending_user_message` 或队列。
4. **控制操作幂等。** 外置前端必须为每次发送提供稳定的 `idempotency_key`；cancel、Stop-and-send 和 drain 都要用它或 `entry_id` 去重。重复请求不得产生第二条 transcript 或第二个 successor worker。没有幂等键时可以保证保留消息，但不能保证严格的“最多发送一次”。
5. **恢复优先于报告成功。** 进程重启、连接断开、取消异常、启动异常和 session 切换都必须能从持久队列恢复完整文本及附件。

## 推荐实现

### 1. 统一 session 控制锁

让 `/api/chat/cancel`、普通 `/api/chat/start` 的 owner 变更、队列入队和队列 drain 都使用 `_get_session_agent_lock(session_id)`。

锁内只做以下工作：重新读取 session、校验 stream/generation、修改控制字段、修改队列、`session.save()`。不得在锁内等待 LLM、SSE 或其它网络调用。

建议固定锁顺序，避免与 streaming worker 死锁：

```text
session_agent_lock
  -> session state / session.save()
STREAMS_LOCK
  -> STREAMS / CANCEL_FLAGS / AGENT_INSTANCES snapshot
ACTIVE_RUNS_LOCK
  -> ACTIVE_RUNS snapshot or owner update
```

取消前先在 session lock 内将状态置为 `cancelling`，记录旧 `stream_id + generation`，释放 session lock 后再执行 Agent 中断。

### 2. 建立 cancel fence 和控制代次

取消开始时原子执行：

```text
assert active_stream_id == requested_stream_id
assert active_stream_generation == requested_generation
cancel_state = cancelling
cancel_stream_id = requested_stream_id
cancel_generation = requested_generation
save()
```

随后所有到达的 `/api/chat/start` 请求都必须在同一把锁内看到旧代次仍处于 `cancelling`，进入队列或返回明确的 `cancel_pending`，不能直接覆盖旧 owner：

```json
{
  "queued": true,
  "status": "cancel_pending",
  "entry_id": "uuid",
  "stream_id": "old-stream-id",
  "retryable": true
}
```

取消终态只允许清理匹配的旧代次。若 successor 已经登记，旧终态必须按 stale event 处理，不能把状态重新写成 idle 或删除 successor 队列。

### 3. 复用现有接口实现 Stop-and-send

本次落地不新增接口路径，也不要求 cancel 携带下一条消息。外置前端继续按原流程调用：

```text
GET  /api/chat/cancel?stream_id=old-stream-id
POST /api/chat/start { session_id, message, attachments?, idempotency_key? }
```

cancel 只负责建立 `cancelling` fence、发出中断信号和等待旧 worker；新消息由现有 `/api/chat/start` 负责持久化排队。若未来希望减少一次请求，可以在同一路径增加 POST cancel 的 `next_message`，但不是本方案的必要条件。

为了兼容外置前端仍然先调用 `/api/chat/cancel`、再调用 `/api/chat/start` 的流程，`/api/chat/start` 必须做以下调整：

- 发现同一 session 仍有 `active_stream_id` 或 `ACTIVE_RUNS` 时，不直接丢弃请求或只返回不可恢复的 409；
- 使用请求中的 `idempotency_key` 把消息写入同一会话的 `queued_user_messages`；服务端不能用文本内容猜测请求是否重复；
- 返回 `202`/`queued` 和 `entry_id`，由后端 drain 在旧 worker settled 后启动；
- 如果队列项已经存在，返回原状态，不重复入队。

返回值区分 `queued`、`dispatching`、`sent` 和错误状态。客户端重试同一幂等键时返回原 entry 状态，不得再次入队。为兼容未升级的外置前端，缺少幂等键时仍入队并返回 `entry_id`，但重复 HTTP 重试可能产生重复 turn。

这样即使 `/api/chat/start` 先于 `/api/chat/cancel` 到达，消息也会先进入持久队列；随后 cancel 只负责设置 fence 和中断旧 worker。若 start 与 cancel 同时到达，二者必须通过同一 session lock 决定唯一顺序。

当前实现的忙时 409 必须改为可恢复的 `202 queued`（或等价的成功排队响应）；如果仍返回 409，且外置前端不重试，就无法从后端证明消息一定不丢。

### 4. 让队列 drain 由后端驱动

不要把“取消终态事件到达浏览器”作为唯一触发条件。后端在以下任一时机检查队列：

- 旧 worker 正常、异常或取消退出；
- `ACTIVE_RUNS` 确认旧 worker 已消失；
- 服务启动恢复 session；
- 外部客户端轮询 session 状态。

drain 协调器在 session lock 内将队首从 `queued` 标成 `dispatching`，写入 lease/attempt token 后释放锁启动 worker。启动流程必须先持久化 successor 的 pending state，再登记 stream owner；两步之间进程退出时，恢复逻辑依据 lease 超时和 session owner 代次把队列项恢复为 `queued`。只有确认 worker 可由 `ACTIVE_RUNS` 追踪后才标记 `sent`。使用 entry lease/attempt token 防止两个 drain 协调器并发发送同一项。

### 5. 修复迟到 worker 的所有权检查

为每个 worker 保存不可变的 `(session_id, stream_id, generation)`。所有 finally 写回均执行：

```text
if session.active_stream_id == stream_id
   and session.active_stream_generation == generation:
    清理当前 owner / pending_user_message
else:
    只 unregister 自己的 ACTIVE_RUNS 条目
```

取消路径也必须在清理前重新读取 session，不能使用锁外快照。这样旧取消响应、旧 SSE 和旧 worker 都无法覆盖 successor。

## 状态清理矩阵

| 退出路径 | `cancel_state` | 队列项 | 当前 owner | 旧 worker |
| --- | --- | --- | --- | --- |
| 取消请求已持久化 | `cancelling` | 保留 `queued` | 标记旧代次 | 允许继续 unwind |
| 旧 worker 终态且无 successor | `settled` | 队列保留 | 清理旧 owner | unregister |
| successor 启动成功 | `idle`/新代次 | 对应项 `sent` | 指向新 stream | 旧 worker 只能清理自身 |
| successor 启动失败 | `settled` | 保留 `queued` 或 `failed` | 不创建伪 owner | unregister |
| cancel 重试/重复请求 | 原状态 | entry 不重复 | 不重复启动 | 幂等 |
| 服务重启恢复 | 按持久状态恢复 | `queued`/`dispatching` lease 超时后重试 | 重新核对 ACTIVE_RUNS | 不信任旧内存状态 |

## 回归测试计划

### 后端并发测试

1. cancel 与 `chat/start` 交错：新消息先进入后端队列，旧 stream 终态不能删除该队列项。
2. 旧 worker finally 晚于 successor：旧 worker 不得清理 successor 的 owner、pending message 或队列。
3. `/api/chat/cancel` 携带 `next_message` 重复提交同一 `idempotency_key`：只产生一个 entry 和一个 successor stream。
4. 新 stream 启动前进程退出：重启后队列项仍为可恢复状态并只发送一次。
5. 启动失败、Agent interrupt 抛错、session 不存在或 stream 已过期：返回明确的结构化错误，用户文本仍可重试。
6. 多标签页/多进程 worker：session lock、entry lease 和完整 session 身份保证不会跨会话发送或重复发送。

### 验收标准

- 取消后 1 秒内提交的新消息，最终必定出现在同一会话的持久 transcript 中，或保持可重试的持久队列项。
- 所有请求交错顺序下，新消息至少发送一次；提供稳定幂等键时最多发送一次。
- 旧取消事件和旧 worker 不能删除、覆盖或重排 successor 状态。
- 服务重启、网络断开和取消失败后，后端仍能恢复完整文本和附件。
- 受影响测试通过 `./scripts/test.sh` 运行；并覆盖正常、异常、取消、替换和 teardown 生命周期出口。

## 实施边界与顺序

建议按以下逻辑拆分提交：

1. 在 WebUI 建立 session 控制状态、generation 和后端持久队列，并补并发测试；
2. 扩展现有 `cancel`/`start` 请求体及幂等 drain，不新增路径；
3. 为所有 streaming finally 和 checkpoint 写回补充 successor ownership guard。

本方案已在 WebUI 后端落地：Session 持久化 `pending_next_turns` 和控制代次；忙时 `/api/chat/start` 返回 202 并入队；旧 worker 退出后由后端 drain；取消 fence、幂等键和 successor 所有权校验均在 WebUI 内完成。未修改关联的 `hermes-agent` 仓库。前端只需消费后端返回的 `queued`/`sent` 状态；即使前端刷新或请求重试，消息可靠性仍由后端持久状态保证。
