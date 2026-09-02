# RFC：线程级 Agent 快速取消方案

- **Status:** Proposed
- **Author:** Hermes WebUI 维护者
- **Created:** 2026-09-02
- **关联 RFC：** [`webui-run-state-consistency-contract.md`](webui-run-state-consistency-contract.md)、[`webui-pending-intent-controls.md`](webui-pending-intent-controls.md)、[`hermes-run-adapter-contract.md`](hermes-run-adapter-contract.md)
- **跨仓库范围：** Hermes WebUI 与 Hermes Agent

## 摘要

本方案保留当前“同一进程内、每轮会话一个 worker 线程”的执行模型，但要求所有
阻塞边界都遵守统一的取消契约。取消请求应快速确认，当前 Provider 请求和工具任务
应立即收到中断信号，只有旧 worker 确认真正结束后，排队的下一轮会话才可以启动。

本方案不尝试强制杀死 Python 线程。Python 无法在保证会话、工作区、Socket、锁和
工具状态安全的前提下终止任意线程。因此，快速取消要求每个阻塞操作至少满足以下
一种条件：

- 支持协作式取消；
- 使用可由其它线程主动中止的请求级 Transport；
- 使用可终止并回收的子进程组；
- 具有明确、有限的执行超时。

WebUI 不能为了让下一轮绕过繁忙检查而提前删除 `ACTIVE_RUNS`。它必须继续表示
worker 的真实生命周期。只有旧 worker、checkpoint 写线程以及所有可能产生副作用
的工具任务都已停止，排队的新轮次才能开始。

## 问题

当前本地默认路径会为每个 stream 创建一个进程内 worker 线程，并在同一会话的不同
轮次之间复用缓存的 `AIAgent`。取消过程实际上存在两个不同的完成点：

1. `cancel_stream()` 持久化取消栅栏，设置 WebUI cancel event，调用
   `AIAgent.interrupt()`，保存部分输出，释放 SSE sidecar，并完成会话取消内容的
   持久化整理。
2. worker 仍然保留在 `ACTIVE_RUNS` 中，直到 `_run_agent_streaming()` 进入最外层
   `finally`，停止 checkpoint 线程、清理运行状态并注销当前 run。

`/api/chat/cancel` 随后会轮询 `ACTIVE_RUNS`，最长等待 10 秒。如果 worker 正阻塞
在 Provider、工具、SDK、远程环境、子 Agent 或清理逻辑中，而这些边界又不能及时
响应 interrupt，用户就会感到取消很慢。

取消期间：

- 普通 WebUI 下一轮请求会被持久化为待发送轮次，返回 `202`；
- 非 WebUI 调用或 `allow_busy_queue=False` 的路径会返回 `409`；
- 排队轮次必须等旧 worker 离开 `ACTIVE_RUNS` 后才能启动。

因此，慢的不是 SSE 断开。SSE sidecar 可以提前释放，真正耗时的是从
“取消请求已接受”到“旧轮次所有执行所有者都已停止”的过程。

## 当前代码中已经具备的能力

本方案建立在现有安全机制之上，不重复实现已经存在的能力：

- `cancel_stream()` 会先写入 `cancel_state=cancelling` 和 generation 栅栏，再向
  Agent 发送中断，避免新旧轮次写回竞争。
- `ACTIVE_RUNS` 表示 worker 生命周期，不表示 SSE 生命周期。
- 同一会话在 worker 退出期间收到的 WebUI 请求会被持久化排队。
- worker 会先注销 `ACTIVE_RUNS`，再尝试发送一个排队的后继轮次。
- `AIAgent.interrupt()` 已经会通知会话执行线程、并发工具线程、活跃子 Agent，
  并调用当前已注册的请求中止函数。
- OpenAI 兼容接口和 Anthropic 主推理路径已经使用请求级客户端，并支持从其它线程
  shutdown 当前 Socket；资源最终由请求所有者线程关闭。
- Terminal 和 Code Execution 已具备中断检查与子进程组终止逻辑。
- 并发工具执行器在短暂等待后可能调用 `shutdown(wait=False)`。不支持 interrupt
  的工具线程可能继续在后台运行。

当前持久化字段 `session.cancel_state=settled` 只表示取消处理已经完成会话与
transcript 的整理，不能证明 worker 已经退出。启动下一轮时仍应以
`ACTIVE_RUNS` 为准；本方案落地后，还要同时检查取消债务是否清零。

最关键的正确性缺口是：并发执行器不再等待某个 Future，并不代表底层线程已经停止。
如果一个可能产生副作用的 detached 工具线程仍在修改文件、远程系统或运行状态，
此时启动下一轮，即使 `ACTIVE_RUNS` 已被删除，实际上仍然存在两个轮次并发写入。

## 目标

- 保持默认本地运行方式为进程内线程模型。
- 取消接口快速返回，不再把完整 worker 退出时间算进普通 HTTP 响应时间。
- 让已支持取消的 Provider、工具、子 Agent 和 checkpoint 快速结束。
- 旧轮次真正安全后，立即且仅启动一个排队的后继轮次。
- 保留原始用户消息、附件、部分回答、reasoning、工具活动和唯一取消终态。
- 阻止旧 generation 在新轮次启动后继续写回会话或产生工具副作用。
- 能定位取消卡在哪个阶段，同时不记录提示词、凭据、工具参数和响应正文。
- 保持 Gateway 取消为独立、失败关闭的控制路径。

## 非目标

- 不把每轮 Agent 迁移到独立进程。
- 不默认启用 Gateway。
- 不使用 CPython 异步异常注入等任意线程强杀方案。
- 不通过提前删除 `ACTIVE_RUNS` 伪造 worker 已结束。
- 不允许旧轮次仍可能产生副作用时启动同一会话的新写入者。
- 不重新定义 Queue、Steer、Stop、Stop-and-send 的产品语义。
- 未通过确定性取消测试的第三方 SDK，不声明为支持快速取消。

## 术语

- **已接受（accepted）：** WebUI 已校验目标 run，持久化取消 generation 栅栏，设置
  本地取消信号，并调用 `AIAgent.interrupt()` 或外部 Runtime stop 控制。
- **SSE 已释放（sidecar released）：** stream 级 SSE 映射已删除，但不表示 worker
  已经停止。
- **已结束（settled）：** 会话 worker、checkpoint 写线程、Provider 请求、子 Agent
  和本轮所有可能产生副作用的工具都不能再修改状态。
- **已认证边界（certified boundary）：** 已通过确定性测试，能在声明的时间预算内
  响应取消并关闭阻塞操作的 Provider 或工具路径。
- **取消债务（cancellation debt）：** 已收到 interrupt，但尚未确认退出或解除资源
  所有权的运行对象。

## 必须保持的不变量

1. **同一会话只能有一个活跃写入者。** 旧 generation 的所有状态修改者结束前，
   后继轮次不能启动。
2. **生命周期必须真实。** 只有 worker 完成清理并且不再拥有副作用任务时，才能删除
   `ACTIVE_RUNS`。
3. **不能伪造 settled。** 放弃等待 Future 或 Executor，不代表底层线程已经停止。
4. **每轮只有一个终态。** 已取消轮次不能随后覆盖为 `done` 或 Provider error。
5. **保留部分输出。** 取消后继续保留当前用户消息、已输出的 assistant 内容、
   reasoning 和工具状态。
6. **写回必须检查 generation。** 延迟到达的 session、checkpoint 或工具写入必须在
   真正修改状态时确认自己仍拥有当前 generation。
7. **Transport 必须是请求级所有权。** interrupt 线程可以中止当前请求的 Socket 或
   Task，但不能关闭其它线程正在使用的共享客户端。
8. **取消必须幂等。** 重复点击 Stop 不能生成多个取消标记、终态、排队轮次或具有
   额外副作用的 abort。
9. **先排队，再启动后继轮次。** 取消期间提交的新输入必须先持久化，旧轮次安全结束
   后只能发送一次。
10. **interrupt 清理必须绑定 generation。** 只有旧 generation 及其所有子所有者都
    注销后，新轮次才能清除 Agent 的 interrupt 状态。

## 方案设计

### 1. 在 Hermes Agent 中引入轮次级取消上下文

每次 `run_conversation()` 启动时创建一个 Agent 所有的 cancellation context，
包含：

- 单调时钟的 `requested_at`；
- 线程安全的 cancelled event；
- 当前 run/generation 身份；
- Provider 请求、工具、子 Agent、远程环境和子进程组的 abort handle；
- 幂等的 `register()`、`unregister()` 和 `abort_all(reason)`；
- 仅用于诊断的所有者类型、名称和取消确认时间。

`AIAgent.interrupt()` 作为统一中断入口：

1. 设置当前 generation 的 cancelled event；
2. 保留现有的线程级 interrupt 传播，兼容尚未迁移的工具；
3. 在锁内复制 abort handle，在锁外执行 callback；
4. 幂等调用所有 abort handle；
5. 向活跃子 Agent 传播中断；
6. 立即返回，不在接口线程中 join 具体执行所有者。

Registry 归属于当前 `AIAgent` 的 run generation，不能新增进程级全局表。旧请求
注销 handle 时必须校验 generation，避免晚到的旧清理删除新请求的 handle。

现有 `_active_request_abort` 可以作为第一个迁移对象接入新的 context，但不应长期
保持为唯一单槽位，因为同一轮可能同时拥有 Provider 请求、多个工具和子 Agent。

### 2. 补齐 Provider Transport 取消矩阵

不重写已经存在的 OpenAI/Anthropic 请求级 abort，而是把它们的资源所有权规则提取
为统一契约，并审计所有主轮次 Provider 路径：

- OpenAI 兼容 Chat Completions；
- Anthropic 与 Bedrock；
- Codex Responses 与 app-server；
- Gemini Native；
- MoA；
- Provider fallback 和 retry；
- 其它参与当前主轮次推理的 Provider Adapter。

每个阻塞请求必须：

- 在进入第一个网络阻塞操作前注册 request-local abort handle；
- 处理“取消先发生、handle 后注册”的竞争；
- 保证跨线程 abort 非阻塞、幂等；
- 用户取消后立即终止 retry 和 backoff；
- 在请求所有者线程的 `finally` 中关闭并回收资源；
- 只注销同一 generation 下属于自己的 handle；
- 将 abort 引发的网络异常归类为 `InterruptedError` 或统一取消终态，而不是
  Provider error；
- 保留有限 Provider timeout 作为最终安全上限。

只有当 fake transport 能分别模拟 connect、首字节等待、stream read、retry backoff
和凭据/fallback 切换，并且取消能在声明时间内释放这些阻塞点时，该 Provider 才能
标记为已认证。

### 3. 在 Hermes Agent 中定义工具取消契约

每个工具必须声明一种取消能力：

| 能力 | 必须满足的行为 |
|---|---|
| `cooperative` | 按有限间隔检查当前 run 的取消事件，返回统一取消结果。 |
| `subprocess` | 注册准确的子进程组，先发送温和终止信号，短暂等待后升级终止，并 wait/reap。 |
| `transport` | 注册请求或异步 Task 的 abort handle，并设置有限 connect/read/total timeout。 |
| `bounded` | 不能主动中止，但有经过评审的有限最大耗时，且 generation 失效后不能执行晚到写入。 |
| `unsupported` | 未改造、隔离或明确接受阻塞限制前，不能进入“支持快速取消”的前台轮次。 |

Tool Executor 必须把当前 cancellation context 传入工具边界。迁移期间保留
`is_interrupted()`，但新增和已改造工具应使用显式 context，避免线程池中的
interrupt 身份和 generation 所有权不明确。

额外规则：

- 调用工具前检查一次取消，提交工具结果或副作用前再检查一次；
- 网络工具使用可取消 Transport 或短周期 interrupt-aware wait，不能进行一次超长且
  不可观察的 read；
- 本地子进程使用独立 process group/session，取消时终止整个组，而不只是 shell 父进程；
- 远程执行后端必须提供 cancel 操作，并限制 cancel/status polling 的时间；
- CPU 与文件循环按有限数据块检查取消事件；
- approval 和 clarify 等待由同一个 context 解除；
- 子 Agent 注册 child abort handle；仍拥有前台副作用的 child 未停止前，父轮次不能
  声明 settled。

当前 `shutdown(wait=False)` 只允许继续用于已证明为只读且有 generation 防护的任务。
可能产生副作用的工具 Future 如果没有确认取消，取消债务就不能归零，当前 run 也必须
继续保留在 `ACTIVE_RUNS`。

### 4. 让 WebUI checkpoint 与收尾流程支持快速取消

Checkpoint 线程也是状态写入者，因此属于 settled 的判断范围。取消路径中的长时间
等待和锁获取应改为 event-aware 循环及有限时间的锁尝试。收到 stop 后必须：

- 不再调度新 checkpoint；
- 当前 checkpoint 只能在通过 generation 校验后完成或放弃；
- 明确确认以后不会再写入；
- 在注销 `ACTIVE_RUNS` 前完成 join。

分别记录以下阶段耗时：

- checkpoint stop；
- last-resort recovery；
- session save；
- journal terminal write；
- stream map cleanup；
- queued turn drain。

这样可以区分 Provider/工具阻塞与 WebUI 清理阻塞，避免仅缩短 HTTP 等待就声称
worker 取消已经变快。

如果收尾发现 Agent 仍有未确认的取消债务：

- 不能复用该 Agent；
- 不能启动排队轮次；
- 所有者停止后，如果 Agent 清理不完整，应从 `SESSION_AGENT_CACHE` 驱逐；
- 下一轮重新构造 Agent，而不是在旧实例上直接清除可能残留的 interrupt 状态。

### 5. `/api/chat/cancel` 在接受取消后快速返回

本地线程模式保持现有响应字段，但把最长 10 秒的同步 settlement 等待改为短暂的
fast-path 观察窗口。建议首个版本使用 250 ms，并把它作为内部常量，不增加用户设置。

Worker 已在窗口内结束时：

```json
{
  "ok": true,
  "cancelled": true,
  "settled": true,
  "stream_id": "...",
  "settle_timeout_ms": 250
}
```

Worker 仍在退出时：

```json
{
  "ok": true,
  "cancelled": true,
  "settled": false,
  "stream_id": "...",
  "settle_timeout_ms": 250
}
```

`cancelled=true` 继续表示目标 run 已找到并接受取消，不表示 worker 已经结束。
`settled` 才是 worker 生命周期结果。

浏览器应把 `settled=false` 当作正常的“正在安全退出”。用户立即提交下一条消息时，
沿用已有的持久化队列并返回 `202`；worker teardown 或 cancel handler 在确认
settled 后发送该轮次。

必须立即执行的外部调用方应先轮询现有 session readiness/status 接口，确认可以启动
后再发送，而不是持续重试 `409`。

缩短 HTTP 等待只能改善取消接口的响应速度。真正让下一轮快速启动，仍依赖本方案
第 1 至第 4 部分。

### 6. Gateway 取消保持独立且失败关闭

当 Gateway 拥有当前 run 时，WebUI 必须继续解析权威 Gateway `run_id`，并调用
Gateway stop。如果已知 Gateway 拥有该 run，但 stop 没有得到确认，WebUI 应返回
有限错误，不能假装本地 Agent interrupt 已经终止外部执行。

本 RFC 不启用 Gateway，也不改变它的默认状态。Gateway 指标应分开记录：

- run_id 解析耗时；
- stop 请求耗时；
- 观察到 Gateway terminal 的耗时。

这些数据不能与本地线程模式的取消指标混合。

### 7. 增加仅用于诊断的取消 Watchdog

当本地 run 超过快速取消预算仍处于 `cancelling` 时，只输出一次结构化诊断：

- stream/session 标识；
- 已等待的取消时间；
- worker 阶段；
- 当前所有者类型及脱敏名称；
- Provider/工具的取消能力分类；
- abort 已发送与已确认时间；
- checkpoint/finalization 阶段；
- 剩余取消债务数量。

禁止记录：

- 用户提示词；
- 工具参数；
- 凭据；
- 包含敏感参数的 URL；
- HTTP 请求/响应正文；
- 文件内容。

重复 Watchdog 日志必须限流。Watchdog 不能：

- 删除 `ACTIVE_RUNS`；
- 关闭共享 Transport；
- 向 worker 注入异常；
- 把排队轮次标记为可启动。

它只负责指出哪个执行边界违反了取消契约。

## 生命周期

```text
用户点击 Stop
  -> 校验 stream 与 profile 所有权
  -> 持久化 cancel generation 栅栏
  -> ACTIVE_RUNS.phase = cancelling
  -> 设置 WebUI cancel event
  -> Agent cancellation context 中止已注册所有者
  -> 释放 SSE sidecar 并保存部分轮次内容
  -> 返回 accepted 和当前 settled 快照

旧 worker
  -> Provider、工具和子 Agent 确认中断并停止
  -> checkpoint 写线程停止
  -> generation-fenced terminal cleanup
  -> cancellation debt 归零
  -> 注销 ACTIVE_RUNS
  -> 发送且仅发送一个持久化排队轮次
```

不能因为 SSE 已删除或 cancel HTTP 已返回，就提前启动排队轮次。

## 状态所有权

| 状态 | 所有者 | 生命周期 | 对 settled 的作用 |
|---|---|---|---|
| cancel fence 与 generation | WebUI session record | 取消开始到终态清理或新 generation | 阻止旧轮次写回和启动竞争 |
| `ACTIVE_RUNS` | WebUI worker lifecycle | worker 启动到真实 teardown | 本地 busy/settled 的权威判断 |
| SSE sidecar maps | WebUI presentation transport | 仅 stream 观察期间 | 可以早于 worker settled 删除 |
| run cancellation context | Hermes Agent run generation | 当前 `run_conversation()` | 管理 abort 注册与取消债务 |
| Provider request client/task | Hermes Agent Provider Transport | 单次请求 attempt | 中止模型网络阻塞 |
| 工具、子进程或远程 handle | Hermes Agent Tool boundary | 单次工具调用 | settled 前停止副作用 |
| checkpoint writer | WebUI worker | 单个 stream | 注销 active run 前必须停止 |
| durable pending turn | WebUI session queue | 入队到已发送或失败恢复 | settled 后仅启动一次 |

初始实现中，`session.cancel_state` 继续表示持久化的取消内容整理状态，不升级为第二个
worker 生命周期真相。

## 取消时间预算

以下指标是已认证本地边界的验收目标。先使用确定性阻塞 fake 验证，再进行真实
Provider 测试：

| 阶段 | 目标 |
|---|---|
| cancel fence、signal 与 abort dispatch | p95 不超过 100 ms |
| 本地 `/api/chat/cancel` 响应 | p95 不超过 500 ms；正常路径不再等待 10 秒 |
| 已认证 Provider 收到 interrupt 后退出 | p95 不超过 1 秒 |
| 已认证工具、子 Agent、checkpoint 确认取消 | p95 不超过 2 秒 |
| 接受取消后本地 worker settled | p95 不超过 2 秒，自动化测试上限 5 秒 |
| settled 后注册排队轮次 | p95 不超过 500 ms |

时间预算不能成为伪造 settled 的理由。某个 SDK 或工具超时后：

- run 继续保持 `cancelling`；
- 排队输入继续安全保存；
- 诊断信息指出未退出的所有者；
- 不提前启动下一轮。

在线程模式下，只有所有前台阻塞边界都通过认证或被隔离到可终止子进程后，才能承诺
全局硬性取消上限。

## 状态空间与失败矩阵

每个实现分片必须覆盖或明确排除：

| 维度 | 必须考虑的情况 |
|---|---|
| backend | local direct、local journal adapter、已配置 Gateway/runner |
| cancel 时机 | Agent 注册前、Provider connect、首字节等待、token stream、retry/backoff、串行工具、并发工具、approval、clarify、子 Agent、checkpoint、final save、done 之后 |
| 工具形态 | 0/1/多个；只读、写入、子进程、网络、远程环境 |
| 生命周期退出 | success、Provider error、tool error、重复 cancel、晚到 cancel、session switch、SSE reconnect、server teardown |
| 后继轮次 | 无排队输入、一个输入、重复 idempotency key、发送失败、失败后恢复 |
| 所有权 | 同/不同 profile、同/不同 session、过期 stream ID、已轮换 generation、已驱逐 Agent |

## 跨仓库实施分片

每个代码分片都需要维护者明确确认。本 Proposed RFC 本身不构成实现授权。

### 分片 0：监控与稳定复现

WebUI：

- 增加 cancel 接受、worker 退出阶段和 queued-turn drain 的耗时字段；
- 增加确定性的阻塞 Provider 与阻塞工具测试夹具；
- 先复现当前慢路径，证明回归测试在修改前会失败。

Agent：

- 盘点所有主轮次 Provider、工具、远程环境、子 Agent 和 retry wait；
- 为每个阻塞点声明取消能力与资源所有者。

### 分片 1：Agent Cancellation Context 与 Provider 认证

Agent：

- 增加 generation-scoped registry，同时保留现有 interrupt flag；
- 首先接入当前 OpenAI/Anthropic abort；
- 逐个认证其余主 Provider 路径；
- 取消时立即结束 retry 和 backoff。

该分片不改变 WebUI 对外行为。

### 分片 2：工具和子所有者认证

Agent：

- 将 context 传入串行和并发工具；
- 改造网络工具与远程环境；
- 证明子进程组能被终止并回收；
- 防止仍运行的 effect-bearing Future 被算作 settled；
- 中断子 Agent 并等待其确认释放所有权。

### 分片 3：WebUI 收尾与 Agent Cache 安全

WebUI：

- 让 checkpoint shutdown 支持快速取消；
- 将 cancellation debt 纳入 worker settled 判断；
- 只有所有者安全后才注销 `ACTIVE_RUNS`；
- 清理不完整时，在下一轮前驱逐缓存的 Agent；
- 保持现有 session、部分输出、journal 与 queue 不变量。

### 分片 4：快速 Cancel 响应与后继轮次

WebUI：

- 缩短本地 cancel settlement 观察窗口；
- 保持响应字段兼容，并如实返回 `settled`；
- 确认 WebUI 输入在退出窗口得到 `202 queued`，而不是 `409`；
- 注销旧 run 后只发送一个 successor；
- 增加 slow-cancel Watchdog。

Gateway 的响应时间在它自己的控制契约完成评审前保持不变。

### 分片 5：默认启用认证门槛

只有满足以下条件后，才默认启用快速响应行为：

- Provider/工具取消矩阵通过；
- 真实取消诊断没有发现 stale writer；
- 相关测试与邻近测试全部通过。

回滚时可以恢复较长的观察窗口，但不能回滚 generation 栅栏、工具取消或
`ACTIVE_RUNS` 的真实所有权。

## 预计涉及的实现位置

最终代码 diff 应保持集中，但初始审计预计包括：

Hermes WebUI：

- `api/routes.py`：cancel 响应时间、readiness 与排队轮次发送；
- `api/streaming.py`：取消分发、checkpoint/finalization、缓存 Agent 安全和
  `ACTIVE_RUNS` teardown；
- `api/config.py`：现有 active-run registry 与临时诊断状态；
- `tests/`：cancel 顺序、settled、`202` queue、`409` readiness、部分输出保存
  和 Gateway 失败关闭测试。

Hermes Agent：

- `run_agent.py` 或小型 Agent Runtime 模块：generation-scoped cancellation
  context；
- `agent/chat_completion_helpers.py` 与 Provider Adapter：请求级 abort 注册和
  取消异常分类；
- `agent/tool_executor.py`：context 传播及运行中 Future 的真实状态处理；
- `tools/interrupt.py`、执行环境、Terminal/Code Execution、网络工具与 delegation
  路径：按能力实施取消；
- Agent 测试：覆盖每一个声明为已认证的 Provider/工具边界。

这是跨仓库运行行为。WebUI 与 Agent 的变更应分别评审和验证，并通过兼容测试证明任意
一侧回滚都不会破坏现有 session。

## 测试计划

### Hermes Agent 测试

- 在 Provider abort handle 注册前和注册后立即取消；
- 使用确定性 fake 分别阻塞 OpenAI-compatible、Anthropic、Gemini、Codex、MoA 和
  fallback attempt；
- 验证 interrupt 线程只 shutdown request-local Socket，最终 client close 由所有者
  线程完成；
- 在 connect、首字节等待、stream read、retry sleep 和 Provider fallback 时取消；
- 取消串行及并发工具，包括一个未开始 Future 和一个正在运行 Future；
- 终止并回收整个子进程树，而不只是父进程；
- 取消网络、远程环境、approval、clarify 和 delegated child 等待；
- 证明 effect-bearing 工具在失去 generation 所有权后不会继续写入；
- 证明重复 interrupt 和晚到 unregister 都是幂等操作。

### Hermes WebUI 测试

- 在 `AGENT_INSTANCES` 注册前取消，以及仅剩 `ACTIVE_RUNS` 时取消；
- 断言 durable cancel fence 先于 Agent interrupt 保存；
- 保留用户输入、附件、部分回答、reasoning、工具调用和唯一 cancel marker；
- fake owner 仍有 cancellation debt 时，`ACTIVE_RUNS` 必须继续存在；
- worker 真实结束前，session readiness 必须为 false；
- worker 退出期间提交下一条 WebUI 消息，断言返回 `202 queued`、不会返回 `409`、
  不发生重叠，随后只创建一个 successor stream；
- 非 WebUI 立即启动在 ready 前返回 `409`，ready 后成功；
- checkpoint write 和 final save 期间取消，不产生旧 generation 覆盖；
- 覆盖 done 后 cancel、重复 cancel、profile/session 切换、SSE reconnect 和 WebUI
  重启恢复；
- Gateway stop 失败时保持 fail-closed，不能回退到本地取消；
- Watchdog 日志只能包含耗时和所有者类别，不能包含提示词、参数、凭据或响应正文。

时间测试应使用 Event/Barrier 和宽松、跨平台的上限。内部顺序应通过确定性同步验证，
不能依赖 sleep。只有公共验收边界适合断言实际耗时。

## 运行验证

自动化测试通过后，使用临时 `HERMES_HOME` 和 `HERMES_WEBUI_STATE_DIR` 进行隔离的
真实模型测试，至少覆盖：

- 模型 streaming 期间取消；
- 带子进程的长时间本地命令执行期间取消；
- 网络工具期间取消；
- 并发工具期间取消；
- 取消后立即提交下一轮。

每个场景记录：

- `accepted_at`；
- 所有者取消确认；
- worker settled；
- `ACTIVE_RUNS` 删除；
- successor 注册；
- terminal journal 状态；
- 是否出现旧 generation 写入。

验证证据中不能包含 Provider 凭据、用户输入或工具 payload。

## 回滚方案

- Cancellation context 是增量机制；迁移期间未适配的 Provider/工具可以继续使用现有
  interrupt flag。
- Provider 和工具认证以小型独立变更落地，可分别回滚。
- WebUI 快速响应窗口单独落地，可在不回滚安全改进的情况下恢复原等待时间。
- 初始分片不要求迁移 session schema；诊断字段应保持为临时内存状态，或使用现有
  run journal 扩展点。
- 如果某个 Provider/工具不能满足契约，将它标记为 unsupported 或恢复其有限等待。
  不能通过弱化 `ACTIVE_RUNS` 或 generation fence 来让延迟指标变绿。

## 评估过的替代方案

### 立即清除 `ACTIVE_RUNS`

拒绝。旧线程或 detached 工具仍可能修改状态，会违反单写入者和真实 settled 不变量。

### 为下一轮创建另一个 Agent 实例

拒绝。同一 session 中，新对象不会终止旧文件、远程系统、transcript 或 checkpoint
写入，反而会形成 split-brain。

### 向 Python 线程注入异常

拒绝。在任意 Python/C Extension 边界都不安全，也无法可靠释放锁、SDK 状态、
子进程和外部资源。

### 将每轮迁移到子进程

暂缓，但不永久否定。独立进程可以提供更强的 hard-kill 边界，但属于
Runner/Sidecar 的执行所有权迁移。本 RFC 优先改善现有线程模式。

### 只缩短 cancel 接口超时

不能作为完整方案。它只能改善接口的表面延迟，不能让 worker 或排队轮次真正更快
启动。

## 待确认问题

- Cancellation context 首版放在 `run_agent.py`，还是放在 Provider、工具和子 Agent
  共同依赖的小型 Runtime 模块？
- 哪些工具属于 effect-bearing，哪些只读工具可以在 generation fence 保护下安全
  detach？
- Slow-cancel 诊断需要写入 journal 参与 replay，还是只写运行日志？
- 250 ms fast-path 观察窗口是否兼容全部现有 API Client，还是应先使用内部 rollout
  flag？
- 哪些非本地执行环境能提供真正的 cancel acknowledgement，而不只是客户端 timeout？
- 同一 Agent 出现多少次取消契约异常后，应在轮次 settled 后自动驱逐缓存？
