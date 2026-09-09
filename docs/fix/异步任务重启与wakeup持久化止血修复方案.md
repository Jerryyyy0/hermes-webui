# 异步任务重启与 Wakeup 持久化止血修复方案

- **状态：** Proposed
- **创建日期：** 2026-09-08
- **适用范围：** Hermes WebUI 异步委派 completion 接收、排队、流式恢复与显示投影
- **关联契约：** [异步委派完成的 Turn 对齐与实时展示](../rfcs/async-delegation-turn-alignment.md)、[WebUI Run State Consistency Contract](../rfcs/webui-run-state-consistency-contract.md)

## 1. 问题与修复边界

Hermes Agent 会把后台委派结果格式化为完整的模型输入，例如：

```text
[ASYNC DELEGATION BATCH COMPLETE — deleg_xxx]
```

这段文本必须进入 Agent 上下文，但不是用户输入，不应出现在 WebUI transcript。
当前链路同时存在以下问题：

1. 异步 wakeup 活跃时，完整 completion anchor 被保存在 `pending_user_message`；
   `GET /api/session` 缺少隐藏语义，前端可能把它恢复成可见 user 消息。
2. Agent durable completion 在 wakeup stream 刚创建时就被 ACK；此后强制重启可能导致
   Agent 认为事件已经交付，而 WebUI 尚未完成处理。
3. session 忙碌时通过约 0.5 秒一次的 claim、release、requeue 等待，造成 SQLite
   `delivery_attempts` 与 session 忙碌时长线性增长。
4. 中断恢复只恢复 assistant/tool 内容，没有稳定恢复 `_turn_key`、`_source` 和
   `delegation_id`。
5. session id 已解析但 sidecar 不存在时，当前兼容分支仍会再次无条件加载 session，
   可能形成无界重排。

本方案只修改 Hermes WebUI，保持新旧 Hermes Agent 兼容，不修改 Agent SQLite schema、
Gateway、CLI、TUI 或 `_format_async_delegation()` 原文。首版只允许 local Agent backend
实际执行异步 wakeup；Gateway/runner 会话只持久接管并保持 queued，不把 501 误记为执行失败。

对于已经开始执行后被强杀的 wakeup，本方案选择**失败关闭**：恢复已产生的可见输出并将
任务标记为 interrupted/failed，不自动重放模型与工具调用，避免重复外部副作用。

## 2. 状态所有权

修复后的状态职责如下：

| 状态层 | 唯一职责 |
| --- | --- |
| Agent `async_delegations` 表 | WebUI 接管前的 durable outbox；ACK 只表示 WebUI 已持久接管 |
| WebUI session sidecar | completion durable inbox、origin turn、wakeup 排队与结算状态 |
| WebUI turn/run journal | 已启动 stream 的 provenance 与部分输出恢复 |
| `/api/session` 等公共投影 | 只返回可显示 pending 内容，不泄露模型内部 completion anchor |
| 浏览器 INFLIGHT 状态 | 恢复可见用户输入和 assistant 流，不承担 durable 消息所有权 |

核心交接顺序：

```text
Agent durable completion
    → WebUI claim
    → WebUI sidecar 持久化 queued inbox
    → ACK Agent completion，并尽可能回读确认 durable delivery_state
    → WebUI scheduler 等待 session 空闲
    → 在 session lock 内原子提交 stream 与 wakeup 运行态
    → 启动 wakeup worker
    → durable 输出结算完成后更新 wakeup 终态
```

关键不变量：WebUI 只有在 sidecar 保存成功后才 ACK Agent。ACK 后即使 `server.py` 退出，
queued completion 仍能从 WebUI sidecar 恢复。对于支持 durable readback 的 Agent，只有
确认 `delivery_state=delivered` 后 scheduler 才把该 inbox 视为可执行；旧 Agent 保持兼容路径。

## 3. 最小 Sidecar 字段

复用现有 `async_delegation_origins[delegation_id]` 中的 `turn_key`、`completed_at`、
`status`、`wakeup_state`、`cancel_state` 和 `child_task_summary`。保留
`wakeup_state` 顶层结构以兼容现有生命周期、SSE 和测试，只新增一个私有 `wakeup`
子对象：

```json
{
  "wakeup": {
    "prompt": "...",
    "stream_id": null,
    "start_attempts": 0,
    "error_code": null
  }
}
```

字段语义：

- `wakeup.prompt`：WebUI 已接管的完整模型输入。只保存在 sidecar，不进入公共 API、SSE、
  export 或 Manifest。
- `wakeup.stream_id`：wakeup 进入原子 admission 后对应的 WebUI stream，用于取消、重启恢复
  和 provenance 定位；尚未进入 admission 时为 `null`。
- `wakeup.start_attempts`：已进入原子 admission、随后尝试 `thread.start()` 的持久化次数。
  session busy、provider paused、backend unsupported 和 ACK 待确认不计数。
- `wakeup.error_code`：稳定的机器可读失败原因，例如
  `server_restarted_during_wakeup`、`start_retry_exhausted`、
  `completion_payload_conflict`、`origin_unresolved`、`agent_ack_pending`、
  `provider_paused`、`backend_unsupported`。

`wakeup` 是 WebUI 私有 durable inbox，不属于公开任务状态。现有公开投影继续只读取
顶层 `status`、`wakeup_state`、`cancel_state`、时间和 child summary。

不新增以下字段：

- 不保存 prompt hash；`delegation_id` 是幂等键，冲突时直接比较已保存 prompt。
- 不新增 completion 接收时间；FIFO 使用现有 `completed_at`，相同时间用
  `delegation_id` 稳定排序。
- 不保存 Agent ACK 时间；ACK 结果写结构化日志，正确性由 sidecar 是否已持久化决定。
- 不新增 run token；WebUI 单 scheduler、session lock 和条件状态更新足以串行化启动。
- 不新增 started/settled 时间；现有 completion 时间、run journal 和日志已可审计。

存储保留规则：

- `queued`、`running`、`failed` 保留 `wakeup`，避免失败时丢失已接管结果。
- `settled` 或完整取消后直接删除整个 `wakeup` 子对象。
- 公共生命周期投影不得包含 `wakeup` 子对象中的任何字段。

## 4. 状态机

沿用现有公开 `wakeup_state`，不增加新的 SSE 枚举值：

```text
idle → queued → running → settled
                    └────→ failed
```

| 当前状态 | 事件 | 结果 |
| --- | --- | --- |
| `idle` | completion 到达并成功写 sidecar | `queued`，随后 ACK Agent |
| `queued` | ACK 尚未确认、session busy、provider paused 或 backend unsupported | 保持 `queued`，不进入启动事务 |
| `queued` | scheduler 选中且启动前置条件全部成立 | 在 session lock 内一次保存 pending、stream id 和 `running` |
| `running` | worker dispatch 抛错且可证明 worker 未启动 | 按同一 delegation、stream、generation 条件回到 `queued` |
| `running` | stream 正常完成且全部 durable 输出已保存 | `settled`，同一次保存中清理私有 prompt |
| `running` | provider/Agent 终态错误 | `failed`，保留私有 prompt和错误码 |
| `running` | server 重启、旧 stream 不存活且 journal 已终态 | 只恢复 durable finalization，再按 journal 终态结算 |
| `running` | server 重启、旧 stream 不存活且 journal 未终态 | `wakeup_state=failed`，写入 `server_restarted_during_wakeup`，保留 delegation `status`，不重放 |
| `queued` | 用户取消 | `wakeup_state=settled`、`cancel_state=cancelled`，不启动 stream |
| 任意终态 | 重复 completion | ACK 重复事件，不创建新 stream |

`running` 的定义改为“启动资格、core pending、stream id 和取消栅栏已一起持久提交”，
不再表示 scheduler 已经先行 claim。不存在启动成功后再 attach stream id 的回写窗口。

非终态重复 completion 只有在已保存 prompt 仍存在时才做逐字冲突比较。`settled` 或完整
取消已清理 prompt 后，同一 `delegation_id` 的重放按终态幂等 ACK，不再声称可比较 payload。

## 5. 代码修改清单

### 5.1 Integration 状态与 Scheduler

在 `integration/async_delegation_turns/` 新增 `inbox.py`，业务逻辑留在 integration 层；
`api/background_process.py`、`api/streaming.py` 和 `api/routes.py` 只保留薄调用接缝。

`state.py` 增加以下原子操作，所有操作均在 session agent lock 内执行并落盘：

- `receive_completion(...)`
  - 验证 delegation origin；
  - 首次收到时保存 `wakeup.prompt`、状态、child summary，设为 `queued`；durable Agent
    同时先写 `wakeup.error_code=agent_ack_pending`，使 ACK 前的默认状态失败关闭；
  - 重复相同 prompt 时返回既有记录，不重复排队；
  - 相同 ID、不同 prompt 时失败关闭，不覆盖原 prompt。
- `select_next_queued_wakeup(...)`
  - 只读选择最早的可执行 queued 记录，不改变 durable 状态；
  - 排序键为 `(completed_at, delegation_id)`；
  - `agent_ack_pending`、provider paused、backend unsupported 或有人类 pending turn 时不可执行；
  - `error_code` 缺失不自动代表 ACK 成功，只有 receive 时已按新格式写入 ACK 初态的记录才
    使用该规则；旧 sidecar 先按 capability 做一次兼容判定。
- `prepare_wakeup_start_locked(...)`
  - 由 `start_session_turn()` 已有 session lock 内的唯一启动接缝调用；
  - 重新验证记录仍为 `queued`、`cancel_state=none`、session 未删除/归档/清空，且
    `active_stream_id`、`STREAMS` 和 `ACTIVE_RUNS` 均没有该 session 的活动所有者；
  - 校验 `turn_key_override` 与 origin turn 完全一致；
  - 分配 stream id，并与 core pending、`running`、`wakeup.stream_id`、control generation、
    `start_attempts + 1` 一起通过一次 `Session.save()` 提交。
- `requeue_unstarted_wakeup(...)`
  - 只在 `thread.start()` 抛错且可证明 worker 未启动时使用；
  - 必须同时匹配 delegation id、stream id 和 control generation，旧 worker 不得回写新 run；
  - 清理对应 provisional stream/core pending 后回到 `queued`，保留累计 attempts 和错误码。
- `record_retryable_start_failure_locked(...)`
  - 只记录尚未进入 admission 的异常或 5xx；必须仍匹配同一 queued delegation；
  - 一次 start 调用最多增加一次，若 admission 已经递增则不得重复计数。
- `settle_wakeup(...)`
  - 正常完成、失败、取消都通过该入口；
  - 只允许匹配当前 delegation、stream id 和 control generation 的 worker 结算；
  - 终态幂等，禁止逆向状态迁移。
- `recover_wakeup_inbox(...)`
  - 启动时恢复 queued；
  - running 且旧 stream 不存活时，有 terminal journal 只恢复 finalization，否则失败关闭。

删除独立的“queued → running claim”和“启动后 attach stream id”两步。为复用当前启动逻辑，
把 `_prepare_chat_start_session_for_stream()` 拆成“只修改对象”和“保存”两个阶段，或给它增加
integration pre-save hook；core pending 与 inbox 运行态必须由同一个锁内 `Session.save()` 提交。

同一锁内先创建并登记 provisional stream channel，再保存 sidecar；保存失败立即撤销该 channel。
锁释放后才 `thread.start()`。这样取消若先获得锁，启动前置条件会失败；启动事务若先提交，
取消一定能看到 stream id、generation 和 channel，并在 worker 入口前设置取消栅栏。

新增嵌套锁只允许 `_get_session_agent_lock` → `STREAMS_LOCK` → `ACTIVE_RUNS_LOCK` 的顺序。
任何已经持有 stream/active-run 锁的取消、回收或 GET 路径必须先取快照并释放，再获取 session
lock；禁止在 shared recovery helper 内反向取锁。

session delete、archive、clear、replace 也必须经过相同 session lock。它们在删除、归档或清空
前先取消已分配 stream 并推进现有 control generation；worker 入口和最终写回都要重验
generation，避免已删除或已归档会话被旧线程复活。

`inbox.py` 提供一个 WebUI 进程内 scheduler：

- 用 condition variable、按到期时间排序的最小堆和 session 去重 map 管理唤醒；scheduler
  线程不直接 `sleep(16)`，等待中的 session 不阻塞其他 session。
- 堆里只保存 session id 与到期时间，durable 数据始终从 sidecar 读取。
- candidate map 是 scheduler 内存状态的唯一所有者，每次入堆带递增 ticket；弹出时忽略过期
  ticket。settled、failed、cancelled、session delete/archive 和正常 shutdown 都要释放对应条目。
- 每个 session 同时最多一个 wakeup stream。
- session 忙碌时保留 queued，不轮询 Agent completion 表，也不增加 `start_attempts`。
- 普通流或 wakeup 流结束、取消、清理后重新通知该 session。
- 每 30–60 秒对进程内候选 map 做一次带抖动的 queued safety sweep，补偿 condition notify
  丢失；完整持久化 sidecar 扫描只在启动或 scheduler 重建时执行，避免常态遍历全部会话。
  lifecycle owner 发现 scheduler 死亡时重建一次，不能只等下次 server 重启。
- server 启动时扫描持久化 sidecar，只调度 ACK 已确认或 legacy-compatible 的 queued 记录。
- 人类 `pending_next_turns` 优先；其队列清空后再按 completion FIFO 启动 wakeup。持续的人类输入
  可能延后后台结果，但不会并发覆盖用户意图，safety sweep 保证队列空闲后最终再次检查。
- `provider_paused` 和 `backend_unsupported` 是可重验 blocker，不是永久过滤条件。配置变更通知
  或 safety sweep 先用当前权威配置重验，条件解除后在锁内清除错误码并重新入堆。
- `agent_ack_pending` 也进入同一到期队列，但只执行 Agent ACK/readback，不调用 chat start，
  不消耗 `start_attempts`。因此 ACK 后 readback 异常即使没有新的 Agent event 重放，也能自愈。
- scheduler 停止时只释放进程内线程/集合，不清除 queued sidecar。

启动生命周期顺序为 `.bak`/index 恢复 → Hermes runtime import 校验与 state 目录就绪 → inbox
raw recovery → inbox scheduler → Agent completion drain → HTTP serve。正常关闭时先停止接收新的
Agent completion，再停止 scheduler；已持久化 queued 不清除，running 交给现有 worker/session
shutdown 与 journal 收尾。

启动失败策略：

- `409 session busy`：保持 queued；依赖活动流终态通知和 safety sweep，不计 attempt。
- `process_wakeup_paused`：保持 queued，记录 `provider_paused`；凭据、模型或 provider 配置变化
  时重新通知，并由 safety sweep 兜底，不计 attempt。
- `501 backend unsupported`：保持 queued，记录 `backend_unsupported`；backend 配置变化或重启
  时重新判定，不进入热循环，不计 attempt。
- 其他确定性 4xx：标记 failed，不自动重试；取消竞态单独收口为 cancelled。
- 异常或 5xx：一次调用只计一次；admission 前通过
  `record_retryable_start_failure_locked()` 计数，admission 后沿用已递增次数。最多 5 次，按
  1、2、4、8、16 秒调度到期时间；耗尽后 `failed/start_retry_exhausted`。

`async_delegation_wakeup` 必须纳入现有 server-wakeup provider pause 语义。新增一个共享
`is_server_wakeup_source()`，由 pause admission、credential revalidation 和 scheduler 共用，
避免当前仅匹配 `source == "process_wakeup"` 导致 paused 分支实际上不可达。

### 5.2 Agent Completion 接收

重构 `api/background_process.py` 的 async delegation 分支：

1. claim Agent event；
2. 解析 `origin_ui_session_id`、session mapping 与 origin turn；
3. 调用 Agent formatter 生成 wakeup prompt；
4. 调用 integration `receive_completion()` 持久化 inbox；
5. 保存成功后调用 `complete_async_delegation_delivery()` ACK Agent；WebUI 包装优先直接调用
   能返回布尔值的 `complete_completion_delivery()`，缺少该能力时才回退旧入口；
6. durable Agent 随即通过 `get_durable_delegation(delegation_id)` 回读确认；
7. 依据 ACK 三态决定重试与调度；
8. 通知 inbox scheduler。

ACK 三态：

- `acknowledged`：`complete_completion_delivery()` 明确返回 `True`，或兼容调用后在有效 claim
  窗口内回读确认 `delivery_state=delivered`；清除 `agent_ack_pending`，允许调度。只有能证明
  本次 claim 完成交接的分支记录 `async_delegation_agent_acknowledged`。
- `ack_failed`：回读仍为 `pending`、读取异常或 ACK 调用明确失败；保留 queued inbox，记录
  `agent_ack_pending`，尽可能 release 当前 claim 并按 durable 机制重试，暂不启动 wakeup。
- `ack_unknown`：已确认 Agent 没有 durable readback 能力、只支持 legacy marker，兼容调用后
  无法证明是本次 claim 完成交接，或一次成功查询确认 durable row 已不存在；持久化
  `agent_ack_unknown`，沿用兼容语义允许调度，但不得伪记 acknowledged。row 不存在时已无 Agent
  outbox 消费者可重复领取，因此允许继续。

不为 ACK 新增 sidecar 字段。durable `receive_completion()` 必须先随 queued inbox 持久化
`wakeup.error_code=agent_ack_pending`；确认 delivered 后再以第二次锁内保存清空，兼容路径则保存
`agent_ack_unknown`。重放事件和启动恢复只对 pending 重新回读。Agent row 缺失、读取异常和
legacy capability 必须分别记录；读取异常失败关闭，成功确认 row 缺失或确实没有 readback
能力才走 `ack_unknown`，不能把“调用未抛异常”当成 ACK 成功。

Agent delivered row 会按时间和数量裁剪，因此已持久化的 `error_code=null` 是 WebUI 自己的
ACK 确认凭据，后续启动不强制依赖 Agent row 继续存在。若进程恰在 Agent ACK 成功后、清除
`agent_ack_pending` 前退出，重启会回读：delivered 时清除 pending，row 已被裁剪时进入
`ack_unknown`，pending 或读取失败时继续保持 queued。

保存失败时必须 release Agent claim 并走现有 durable retry，禁止 ACK。

删除以下旧行为：

- session 忙碌时每 0.5 秒 claim/release/requeue；
- wakeup stream 返回 2xx 后才 ACK；
- 把 Agent completion queue 当成 WebUI 等待队列。

兼容路径：

- 新 Agent 使用现有 claim/complete/release API。
- 旧 Agent 继续走 legacy delivered marker。
- ACK 失败导致事件重放时，`receive_completion()` 幂等命中既有 inbox，只重试 ACK。
- terminal duplicate 在 prompt 已清理后按 delegation id 幂等 ACK，不重新格式化或启动。
- exact `origin_ui_session_id` 继续优先于 mutable session-key index。
- session mapping 暂未出现时继续有界重试。
- session id 已解析但 sidecar 不存在时进入同一有界 unresolved 流程，不再第二次无条件
  `get_session()`。

### 5.3 强制重启恢复

启动顺序固定为：先完成现有 `.bak`/state index 恢复和 Hermes runtime import 校验，等 state
目录就绪后执行 inbox raw scan，然后启动 inbox scheduler，最后启动普通 Agent completion drain。
scan 必须使用 `Session.load()` 原始读取，不得调用会触发 stale repair 的 `get_session()` 或
`get_session_for_scan()`。

raw scan 只负责发现候选。任何修改前都要获得 session agent lock 并再次 `Session.load()`，
避免启动扫描用旧快照覆盖并发写入：

- `queued`：重新通知 scheduler。
- `running` 且旧 stream 不在当前活动集合：先读取 run journal。已有 terminal 事件时只重跑
  幂等的 transcript/artifact finalizer，成功后按 journal 的 completed/error/cancel 结果结算；
  没有 terminal 事件时恢复部分输出并将 wakeup 标记为
  `failed/server_restarted_during_wakeup`，同时保留 delegation 原有 `status`。两者都不得
  重放模型或工具调用。
- `settled`、`failed`：不调度；`cancel_state=cancelled` 的记录按 settled 处理。

新增共享 `reconcile_async_wakeup_before_stale_cleanup()` 接缝，所有 generic stale-stream 修复
必须先调用它。至少覆盖：

- `api/models.py::_repair_stale_pending()`；
- `api/models.py::_sync_sidecar_from_state_db_if_newer()`；
- `api/routes.py::_clear_stale_stream_state()`；
- worker exception/final fallback 和 server 启动恢复。

当 `pending_user_source=async_delegation_wakeup` 时，共享接缝在 generic repair 清空 pending 前，
先按 stream id、唯一 running 记录、journal provenance 的顺序定位 delegation，恢复 assistant/tool
journal 并保存 origin turn 归属。terminal journal 进入幂等 finalizer；非 terminal journal 才将
inbox 结算为 interrupted/failed。只有该保存成功后，generic repair 才能清除
`active_stream_id` 和 `pending_user_*`，且不得把 hidden anchor 物化成可见 user。

若 provenance 无法唯一确定，记录 `legacy_provenance_unresolved` 并失败关闭；仍保留 inbox prompt，
但 generic 层可以在同一次锁内保存后清理死 stream/pending，避免 UI 永久 reconnect。共享接缝
不得再次获取非重入 session lock；调用方必须传入已锁定、刚从磁盘加载的 Session。

旧 sidecar 兼容：

- 若恰好一个 delegation 为 `running`，同时 session 的
  `pending_user_source=async_delegation_wakeup`，在清理 pending 前将其
  `pending_user_message` 和 `active_stream_id` 迁移到该 delegation。
- queued 旧记录若没有 prompt，等待 Agent durable event 重放来补齐。
- 无法唯一关联的旧 running 记录标记为 `failed/legacy_provenance_unresolved`，不根据
  completion 文案猜测 delegation id。

### 5.4 Pending 公共投影

为 `/api/session` 和 `/api/sessions` 使用同一个 pending 投影 helper，禁止直接返回 Session
原始 pending 字段。

普通用户 pending：

```json
{
  "pending_user_message": "用户输入",
  "pending_user_source": "webui",
  "pending_turn_key": "turn:2",
  "pending_user_visible": true,
  "has_pending_user_message": true
}
```

异步 wakeup pending：

```json
{
  "pending_user_message": null,
  "pending_attachments": [],
  "pending_user_source": "async_delegation_wakeup",
  "pending_turn_key": "turn:1",
  "pending_user_visible": false,
  "has_pending_user_message": true
}
```

规则：

- `has_pending_user_message` 表示服务器存在活动输入，不能因隐藏而改为 `false`。
- 分类只依赖持久化 source/语义，不匹配 completion 正文。
- `static/ui.js` 的 pending helper 在 `pending_user_visible=false` 时返回 `null`。
- 前端同时检查 `pending_user_source=async_delegation_wakeup`，作为旧响应格式的防御。
- 普通用户 INFLIGHT、刷新和切换会话恢复行为保持不变。
- completion anchor 不产生普通 user SSE message。

### 5.5 Turn Journal 与恢复归属

现有 turn journal `submitted` 事件已经保存 `turn_key`，只补充两个字段：

```json
{
  "source": "async_delegation_wakeup",
  "delegation_id": "deleg_xxx"
}
```

不重复保存 `origin_turn_key`；现有 `turn_key` 就是权威 origin turn。

增加统一 provenance helper，解析顺序为：

1. sidecar 中 `wakeup.stream_id` 匹配的 delegation；
2. 当前 pending source/turn 与唯一 running delegation；
3. turn journal 的 `source`、`delegation_id`、`turn_key`；
4. 无法确认时失败关闭并记录 unresolved，不猜测正文。

该 helper 应用于：

- journal 恢复的 assistant；
- journal 恢复的 tool card/tool message；
- 正常流最终合并；
- 取消、中断和错误 marker；
- Manifest、artifact、MEDIA 和 References 的 turn 归属。

异步可见输出统一携带：

```json
{
  "_turn_key": "turn:1",
  "_source": "async_delegation_wakeup",
  "delegation_id": "deleg_xxx"
}
```

hidden completion user anchor 保留 Agent 上下文语义，但所有显示投影继续过滤。

终态 durable barrier 按现有持久化能力严格按以下顺序执行：

```text
transcript/context/tool_calls 已合并并保存
    → artifact/MEDIA/References 已完成结算并保存
    → turn journal completed 与 run journal done 已持久化
    → run journal + session SSE 发布 async_turn_committed
    → settle_wakeup() 条件更新终态
    → 同一次 Session.save() 删除 settled/cancelled 的 wakeup.prompt
    → worker teardown 并从 ACTIVE_RUNS 注销
    → 通知 scheduler 检查下一条 queued
```

`settle_wakeup()` 必须验证 delegation id、stream id 和 control generation，防止旧 worker 结算
新 run。任何 transcript 或 artifact 结算失败都不得先写 `settled` 或删除 prompt；可以在保存
已恢复输出后进入 `failed/finalization_failed`，但 prompt 继续保留以便审计和人工恢复。

terminal journal 只在 transcript 和 artifact 已成功持久化后写入，因此它本身就是可重跑
finalizer 的 durable 门槛；run journal `done` 还保留终态 session payload 供 replay。重启恢复
只允许重跑这个幂等 finalizer；若 journal 不完整，失败关闭并保留 prompt，
不能为了得到最终回答再次调用模型或工具。

若终态保存时已经把 session 加入 scheduler 候选 map，也必须等 `ACTIVE_RUNS` 注销后才通过
admission guard；candidate notify 不是“session 已空闲”的证明。

### 5.6 取消与清理

- completion 到达前已请求取消：记录 completion 终态并 ACK Agent，不写 prompt、不启动流。
- queued 时取消：直接 settled/cancelled，清除 prompt，不启动流。
- running 时取消：原子启动已保证 stream id 与 channel 同时可见；沿用现有 stream cancel，
  worker 即使尚未进入模型调用也必须先观察 cancel generation，最终通过统一 settle 入口结算。
- session 删除：停止该 session 的进程内调度；Agent 事件若尚未被 WebUI 持久接管，继续由
  durable unresolved 流程处理。
- stream replace、provider error、worker exception 和 server 正常关闭均不得清除 queued
  inbox。

### 5.7 Backend 能力边界

首版执行矩阵如下：

| Backend | durable 接管/ACK | 自动 wakeup 执行 | 处理方式 |
| --- | --- | --- | --- |
| local Agent | 支持 | 支持 | 按本方案原子启动 |
| Gateway | 支持 | 不支持 | 保持 `queued/backend_unsupported`，不调用 501 路径 |
| runner-local | 支持 | 不支持 | 保持 `queued/backend_unsupported`，不调用 501 路径 |

scheduler 在启动事务前只解析一次权威 backend，并把同一个 resolved value 传给 capability
判断、`_start_run()` 与最终 worker dispatch；不得在决定后重新读取配置而切换 backend。不支持时
只更新稳定错误码，不消耗 attempt，不发布 `server_turn_started`，也不宣称 completion 已执行。
以后补齐 Gateway hidden anchor、origin turn 和 delegation provenance 后，可扩展同一能力检查，
不另写平行 scheduler。

## 6. 公共接口与兼容性

- 不新增 HTTP endpoint。
- `/api/session` 与 `/api/sessions` 新增 `pending_user_visible`、
  `pending_user_source`、`pending_turn_key`。
- hidden pending 的 `pending_user_message` 改为 `null`，这是有意的公共显示投影修复。
- SSE schema version 不变，`wakeup_state` 不新增公开枚举值。
- Hermes Agent 无需同步升级；不修改 Agent 仓库。
- 不更新 integration Swagger，因为没有新增或修改 integration HTTP 路由。
- 不修改根 `CHANGELOG.md`；实现 PR 的 Fork 侧发布说明写入 `integration/CHANGELOG.md`。

## 7. 可观测性

增加以下结构化日志事件：

```text
async_delegation_inbox_received
async_delegation_agent_acknowledged
async_delegation_agent_ack_failed
async_delegation_agent_ack_unknown
async_delegation_wakeup_queued
async_delegation_wakeup_admitted
async_delegation_wakeup_started
async_delegation_wakeup_settled
async_delegation_wakeup_interrupted
async_delegation_wakeup_backend_deferred
async_delegation_scheduler_recovered
async_delegation_payload_conflict
async_delegation_origin_unresolved
```

日志只能记录 session id、delegation id、stream id、状态、attempt 数和错误码，不记录完整
`wakeup.prompt`。`started` 只在 `thread.start()` 成功返回后记录；锁内 durable 提交使用
`admitted`，二者不可混用。

## 8. 自动化测试清单

### 8.1 Pending 投影

- 普通用户 pending 在 detail/list API 中仍可见。
- async pending 的正文和附件在 detail/list API 中均被隐藏。
- hidden pending 的 `has_pending_user_message` 仍为 `true`。
- 前端刷新、INFLIGHT 恢复和会话切换不创建 completion user bubble。
- 前端收到旧式响应但 source 为 async wakeup 时仍能隐藏。

### 8.2 Durable Inbox 与 ACK

- sidecar save 严格发生在 Agent ACK 之前。
- sidecar save 失败必须 release，不得 ACK。
- Agent `complete_event_delivery()` 未抛异常但 durable row 仍为 `pending` 时判为
  `ack_failed`，不能记录 acknowledged，也不能启动 wakeup。
- durable row 为 `delivered`、仍为 `pending`、读取异常、row 缺失、legacy 无 readback
  能力分别覆盖，不能把 unknown 与 success 合并。
- ACK 失败后的重复 completion 不产生第二个 wakeup，只重试 ACK。
- ACK/readback 首次失败且 Agent 不再投递事件时，scheduler 的 ACK retry 仍可解除 pending。
- 同一 ID、相同 prompt 幂等；不同 prompt 失败关闭。
- terminal duplicate 在 prompt 已清理后按 ID 幂等 ACK，不误报 payload conflict。
- session 忙碌期间不重复 claim Agent，delivery attempts 不随等待时间增长。

### 8.3 Scheduler 与状态机

- 同 session 多 completion 按 `(completed_at, delegation_id)` FIFO。
- 同 session 不创建并发 stream。
- scheduler 选中 queued 记录本身不改变 durable 状态。
- core pending、stream id、wakeup running、generation 和 attempt 只发生一次 sidecar save；
  测试在任一字段写入处注入异常都不能观察到半套状态。
- cancel 在原子提交前获锁时不启动；cancel 在提交后获锁时一定能按 stream id/generation
  阻止 worker 进入模型或工具调用。
- session delete、archive、clear、replace 与启动并发时，旧 worker 不能复活会话或写回新
  generation。
- 活动流终态会唤醒下一条 queued completion。
- `thread.start()` 抛错只按相同 delegation/stream/generation 回到 queued；旧启动失败不能
  撤销更新的 run。
- 409 busy、paused、backend unsupported、5xx、取消和重试耗尽符合约定状态，前三者不增加
  `start_attempts`。
- 丢弃一次终态通知后，safety sweep 仍能启动 queued；模拟 scheduler 线程异常后 lifecycle
  owner 能恢复线程。
- 一个 paused/busy session 不阻塞另一个 session 到期执行。
- 有人类 `pending_next_turns` 时先启动人类 turn，其队列清空后再启动最早 wakeup。

### 8.4 重启恢复

- queued completion 在重启后只启动一次。
- running completion 在旧 stream 消失后不自动重放：有 terminal journal 时只恢复 finalization，
  无 terminal journal 时变为 interrupted/failed。
- 部分 assistant/tool 从 journal 恢复并绑定 origin turn。
- settled/cancelled/failed 记录重启后不再启动。
- 旧 sidecar 可唯一关联时迁移；无法关联时失败关闭。
- 启动 raw scan 不触发 `_repair_stale_pending()`，也不把 hidden anchor 物化为 user message。
- 分别从 `_repair_stale_pending()`、`_sync_sidecar_from_state_db_if_newer()`、
  `_clear_stale_stream_state()` 和 worker fallback 进入时，都先结算对应 inbox 再清 pending。
- generic repair 与 inbox recovery 锁竞争时不死锁、不用锁外快照覆盖新状态。
- journal terminal 已保存但 transcript 尚未保存、transcript 已保存但 artifact 尚未结算、
  artifact 已结算但 wakeup 尚未 settle 三个强杀边界均可恢复；prompt 不能提前删除。

### 8.5 路由与版本兼容

- 修复当前 async delegation WebUI bridge 中 session 缺失导致的失败测试。
- exact origin 优先，不能跨 session 投递。
- session 缺失采用有界 unresolved，不形成永久 requeue。
- 覆盖 current durable API、legacy marker、无 durable API 三种 Agent 能力组合。
- local Agent 能启动；Gateway 与 runner-local 保持 `queued/backend_unsupported`，不调用当前
  501 路径、不计 attempt、不发送 `server_turn_started`。
- backend 从 unsupported 切换为 local 后，配置变更通知或 safety sweep 可启动既有 queued。
- `process_wakeup` 与 `async_delegation_wakeup` 共用 provider pause 分类，凭据恢复后均可重调度。

### 8.6 语义不变量

- `/api/session`、分页、SSE、replay、export、Manifest 均不含 completion anchor。
- assistant/tool/artifact/MEDIA/References 均属于 origin turn。
- 普通用户重复发送相同正文时不被误去重。
- 对每个成功、错误、取消、中断、replace、删除和 server teardown 出口，断言 wakeup 状态、
  core pending、stream channel、scheduler map 和 prompt 保留/清理规则一致。

### 8.7 确定性强杀注入

使用 isolated `HERMES_HOME` 与 `HERMES_WEBUI_STATE_DIR` 启动子进程，不接触真实会话状态。
通过 Event/Barrier 或测试专用 crash hook 覆盖以下持久化边界，先证明旧实现会失败，再验证修复：

1. inbox sidecar save 后、Agent ACK 前；
2. ACK 已确认后、scheduler notify 前；
3. scheduler 选中后、原子启动提交前；
4. pending/running/stream id 原子提交后、`thread.start()` 前；
5. worker 已启动、`start_session_turn()` 返回前；
6. journal terminal 后、transcript 保存前；
7. transcript 保存后、artifact/MEDIA/References 结算前；
8. durable 输出全部完成后、`settle_wakeup()` 前。

每个边界重启后都断言：queued 要么继续一次，要么已越过 admission cutover 后失败关闭；
running 不自动重放工具；visible output 不丢；hidden prompt 不进入公共投影；同一 delegation
不产生第二个 stream。

相关测试通过后执行完整 `./scripts/test.sh`，要求当前异步桥接红测恢复且不引入新失败。

## 9. 手动验收

1. completion 在普通流期间到达：只显示 queued 状态，普通流结束后启动 wakeup。
2. queued 且 ACK 已确认后强制重启 `server.py`：重启后只启动一次，页面不出现 completion
   user bubble。
3. 在原子 admission 前后分别强制重启：前者保持 queued 并继续一次；后者按 running
   interrupted 失败关闭，不重复执行工具。
4. 流式期间刷新、切换会话和 SSE 重连：只恢复 assistant 活动，不显示内部 prompt。
5. 连续完成两个 delegation：按 FIFO 顺序执行，无并发 stream。
6. 检查 Agent `delivery_attempts`：不再随 session 忙碌时间线性增长。
7. 让一个 session 处于 paused/busy，确认另一个 session 的 wakeup 不受阻塞。
8. Gateway/runner 会话接收 completion 后保持 queued，不出现永久 failed 或虚假 started。
9. 杀掉或模拟 scheduler 线程退出，确认 safety sweep/lifecycle owner 能恢复调度。
10. 验证普通用户消息的刷新重连行为没有退化。
11. 按 UI/UX 规范提供桌面、窄屏和移动端前后截图。

## 10. 明确保留的限制

- 本次不修 Hermes Agent Gateway 抢占 WebUI completion 的消费者归属问题；相关 warning
  可能仍偶发。首版也不在 Gateway/runner 执行 async wakeup，已接管记录保持 queued，等待
  backend 切回 local 或后续补齐 Gateway provenance 能力。
- 对已经开始执行后被强杀的 wakeup 不承诺自动续跑或 exactly-once 执行，统一失败关闭。
- 原子 admission 已提交、但 worker 线程尚未来得及启动时若进程被强杀，也按“可能已开始”
  失败关闭。这是无 run token 前提下选择的 at-most-once cutover，避免重复外部副作用。
- 本次不增加用户手动重试入口。
- 不依赖 completion 文案内容做分类、路由、幂等或恢复。
