# Hermes Agent 侧上下文压缩逻辑说明

本文从 WebUI 集成和会话恢复的角度，梳理 `hermes-agent` 当前的上下文压缩实现。
重点是“什么时候触发、压缩什么、怎样保留上下文、怎样落盘和切换会话”，不展开具体
Provider SDK 的请求适配。

> **代码基线**：本文按本地 `hermes-agent` 仓库提交 `02aefe0042`（`fix(context): preserve durable compaction anchors`）整理。Agent 后续修改压缩器、Gateway 会话存储或 `ContextEngine` 接口时，应同步复核本文。

## 一句话结论

Agent 的压缩不是简单删除旧消息，而是一次“上下文重建”事务：在系统提示词和近期消息之间生成一个结构化摘要，保留工具调用边界与当前任务锚点，再把结果写回 Agent 的会话存储。默认实现是有损摘要，但原始消息在原地压缩模式下仍保留为非活动历史；传统轮转模式则把原会话封存并创建一个带父子关系的 continuation session。

## 1. 整体分层

```text
API 响应 / 工具循环
        │
        ├─ token usage → ContextEngine.update_from_response()
        ├─ turn-start / pre-API 粗略估算
        └─ error recovery / tool-loop pressure check
                    │
                    ▼
        ContextEngine.should_compress()
                    │ 达到阈值
                    ▼
        AIAgent._compress_context()
                    │
                    ▼
        conversation_compression.compress_context()
          ├─ 并发锁、冷却和失败保护
          ├─ ContextEngine.compress() 生成新 message list
          ├─ memory / context-engine 边界通知
          └─ state.db 原地归档或会话轮转

Gateway 收到新消息前还有一层独立安全网：
Session Hygiene（通常按模型上下文的 85% 粗略估算触发）
```

上下文管理通过 `agent/context_engine.py` 的 `ContextEngine` 抽象层接入。默认引擎是
`agent/context_compressor.py` 的 `ContextCompressor`；插件可以替换整个引擎，因此
Gateway 和 Agent 主循环应调用抽象接口，不应假设一定存在默认压缩器的私有方法。

## 2. 触发入口与判定

### 2.1 正常 Agent 压缩

每次模型调用后，Agent 将 Provider 返回的 `prompt_tokens` 等 usage 交给
`ContextEngine.update_from_response()`。默认压缩器主要使用 `prompt_tokens` 判断上下文
压力；completion/reasoning token 不直接代表下一次请求的输入窗口。

默认阈值可理解为：

```text
threshold_tokens = effective_input_window × compression.threshold
```

其中 `effective_input_window` 会扣除 Provider 为 `max_tokens` 预留的输出空间；阈值还受
最小上下文下限和小窗口模型的触发比例保护。默认配置通常是 `threshold: 0.50`，但实际
阈值应以压缩器解析出的 `threshold_tokens` 为准。

### 2.1.1 阈值的实际计算规则

配置中的 `compression.threshold` 是比例，不是最终 token 数。内置
`ContextCompressor` 按以下顺序解析最终阈值：

1. 读取全局比例，默认值为 `0.50`。
2. 应用模型/路由覆盖，但只在覆盖规则允许时生效：Arcee Trinity Large Thinking 使用
   `0.75`；Codex OAuth 路由上的 gpt-5.4/gpt-5.5/gpt-5.6 使用 `0.85`；Codex OAuth
   路由上的 gpt-5.3-codex-spark 使用 `0.70`。Codex 的 `0.85` 覆盖默认开启，可通过
   `compression.codex_gpt55_autoraise: false` 关闭；覆盖只会提高用户配置，不会把用户
   已设置的更高比例降低。gpt-5.3-codex-spark 的 `0.70` 不受这个开关影响。
3. 对上下文窗口小于 `512K` 的内置压缩器，比例至少提高到 `0.75`，避免在
   `128K`–`262K` 等窗口上过早压缩。这个保护是 raise-only：用户或模型已经设置更高比例
   时保持更高值；`512K` 及以上窗口不自动套用该保护。
4. 计算有效输入窗口：

   ```text
   effective_input_window = context_length - max_tokens
   ```

   `max_tokens` 是 Provider 从同一上下文窗口中预留给输出的空间。未设置、非正数或
   无法解析时按没有预留处理；如果扣除后窗口不为正，也回退到原始
   `context_length`。因此，`max_tokens` 较大时，压缩会按更小的输入预算提前触发。
5. 计算比例值后，最终 token 阈值至少为 `64,000`：

   ```text
   threshold_tokens = max(
       floor(effective_input_window × effective_threshold_percent),
       64,000,
   )
   ```

   但如果这个 `64,000` 下限已经达到或超过有效输入窗口，阈值改为有效输入窗口的
   `85%`，且最多为 `effective_input_window - 1`。这样 `64K` 等小窗口仍能在 Provider
   拒绝请求前触发压缩，而不会因为下限等于整个窗口导致永远无法触发。

当真实 Provider usage 可用时，自动压缩的判定条件是
`prompt_tokens >= threshold_tokens`；没有真实 usage 时才使用包含 system prompt 和工具
schema 的粗略请求估算。`completion_tokens`、reasoning tokens 和累计输出 token 不会直接
作为这个输入窗口阈值的判定值。

例如，在没有 `max_tokens` 预留且没有模型覆盖时，`128K` 窗口会因小窗口保护按至少
`75%` 计算，阈值约为 `96K`；`512K` 窗口不适用该保护，默认阈值约为 `256K`。这些
百分比只用于解释阈值来源，运行时决策和 WebUI 展示都应使用压缩器实际产出的
`threshold_tokens`，不能由前端自行用窗口大小反推。

上述规则只适用于内置 `ContextCompressor`。如果通过 `context.engine` 选择了插件引擎，
压缩比例、阈值和边界算法由插件自己负责，宿主的 `compression.threshold` 以及 Codex
自动提高规则不会强行覆盖插件。切换模型时，内置压缩器会按新模型的
`context_length`、输出预留和模型覆盖重新计算阈值，并清除旧模型的 usage 校准状态。

触发检查有多个时机：

1. **turn-start preflight**：新一轮开始时，用系统提示词、工具 schema 和消息列表做粗略请求估算，防止上一轮留下的超大历史直接进入 Provider。
2. **pre-API pressure check**：本轮工具结果刚追加、下一次 API 调用尚未发出时再次估算，捕获单轮工具输出突然撑大的情况。
3. **tool-loop / post-response check**：工具循环继续前，优先使用最近一次真实 `prompt_tokens`；Provider 不返回 usage 时退回粗略估算。
4. **错误恢复路径**：上下文溢出或可归类为应压缩的 Provider 错误时，复用同一个 `_compress_context()`，而不是另起一套删消息逻辑。

粗略估算可能因工具 schema 偏大而高估。成功压缩后，Agent 会等待下一次真实 Provider usage；
如果最近一次真实请求已经低于阈值且粗略估算只小幅增长，会暂缓重复压缩，避免“同一上下文
连续压缩”。

### 2.2 Gateway Session Hygiene

Gateway 在把历史交给临时 Agent 前还有一层会话清理：

- 主要用于消息平台中隔夜积累、跨轮增长或逃过 Agent 工具循环检查的会话。
- 默认安全阈值高于 Agent 主压缩器，通常按模型上下文的约 85% 触发。
- 优先使用上一轮真实 `prompt_tokens`，没有真实 usage 时使用粗略估算。
- 仅在历史足够长时运行，并把它当作 pre-agent 安全网，而不是正常压缩的唯一入口。

这两层都可能最终调用 `AIAgent._compress_context()`，所以压缩事务本身必须具备会话锁、
幂等检查和失败不破坏原历史的能力。

### 2.3 Codex App Server 特例

当 `api_mode == "codex_app_server"` 时，真实线程上下文由 Codex Agent 管理。若配置为
`compression.codex_app_server_auto: native` 或 `off`，Hermes 不会用本地摘要器压缩镜像消息；
在 `hermes` 模式下才走 Hermes 的压缩路径。原因是本地摘要只改镜像，不能缩小 Codex
App Server 的真实线程上下文。

## 3. 默认 `ContextCompressor` 的算法

`ContextCompressor.compress()` 的输入是当前 message list，输出是合法且更短的 message
list。核心流程如下。

### 阶段 A：廉价预处理

压缩前先处理历史负担较重的内容，不调用 LLM：

- 只处理保护尾部之外的旧 tool result。
- 较大的终端输出、文件内容、搜索结果等会被压成带工具名、参数和结果摘要的一行。
- 旧图片会被替换为短文本占位；最新含图片的用户消息作为媒体锚点，避免历史 base64 永久留在每次请求中。
- 该阶段尽量保留“做了什么、结果怎样、错误是什么”，不是盲目全部改成同一个 placeholder。

### 阶段 B：划分 head / middle / tail

```text
system prompt + 首次上下文（head，保护）
                │
                ├─ middle：交给摘要模型
                │
近期 tail（按 token 预算保护，至少保留有限数量消息）
```

主要规则：

- system message 永远属于 head；首次压缩还会额外保护 `protect_first_n` 条非 system 消息。
- `protect_first_n` 不会永久累积：首次压缩后会衰减为 0，避免早期消息在每次压缩中重复复制，导致 head 越来越大。
- tail 的主规则是 token budget，约为 `threshold_tokens × target_ratio`；`protect_last_n` 是近期消息数量的下限和短会话保护，不能理解成“只按消息条数切”。
- 边界不会切在 `assistant(tool_calls)` 与其 `tool` result 之间：必要时向前/向后对齐，避免产生孤立工具调用。
- 最近一个真实 user turn 必须留在 tail，防止摘要中的“历史待处理请求”被误当成当前任务而丢失。
- 最近一个可见 assistant 回复也尽量留在 tail，防止用户刚看到的回答被折叠成不可读的压缩摘要。
- 当 user + assistant + tool result 构成完整 turn pair 时，边界会尽量保持整对进入同一侧。

如果 head/tail 已经占满可用空间，没有可压缩的 middle，压缩器返回原列表并累计一次
无效压缩计数；这会触发后续 anti-thrashing 保护，而不是无限重复 LLM 调用。

### 阶段 C：生成结构化摘要

middle 会被序列化后交给 `call_llm(task="compression")`。摘要提示词要求模型输出面向
恢复的 checkpoint，而不是回答历史用户问题，重点保存：

- 当前目标和未完成任务
- 已完成动作、进行中动作和阻塞项
- 关键决策及原因
- 相关文件、路径、命令、测试结果和错误细节
- 下一步和当前工作区状态

摘要输入还会受到以下约束：

- 每条消息和总输入有截断上限，避免“压缩器本身把整个历史重新吃满”。
- 发送前和返回后都做敏感信息清理。
- 若用户执行 `/compress <focus>`，focus 相关内容占摘要预算约 60–70%，其他内容更激进地压缩。
- memory provider 可以通过 `on_pre_compress()` 提供额外上下文；Agent 会先清理并限制长度，再传给支持该参数的 context engine。
- 后续压缩会发现已有的 `compaction_summary`，采用迭代更新：保留仍有效的旧信息，把已完成项移动到 Completed，合并新进展并去掉过时内容。

摘要预算随被压缩内容量变化，受最小值、模型上下文 5% 和绝对上限共同约束；它不是把
整个模型上下文都分配给摘要。

### 阶段 D：摘要失败分支

摘要调用失败时，当前实现不是单一策略：

| 情况 | 行为 | 是否丢弃 middle |
| --- | --- | --- |
| 认证、权限或不可恢复 quota 错误 | 终止本次压缩，保留原消息 | 否 |
| 网络/连接/流中断 | 终止本次压缩，等待重试 | 否 |
| 其它错误且 `abort_on_summary_failure: true` | 终止本次压缩 | 否 |
| 其它错误且配置允许兜底 | 生成确定性 fallback handoff，再压缩 | 是，信息不完整 |

配置的辅助摘要模型失败时，若主模型可用，压缩器可能先退回主模型重试；这会成功但会
记录“配置的 auxiliary compression model 失败”的告警。摘要失败会进入 cooldown，自动压缩
暂缓；手动 `/compress` 使用 `force=True` 可立即重试。

确定性 fallback 不是完整摘要，只保留可从消息中可靠提取的用户任务、文件/工具/错误等
锚点，并限制最大长度。调用方应向用户显示“摘要不完整”的提示，不能把它当作正常摘要成功。

### 阶段 E：组装和消息语义

压缩结果按以下顺序组装：

1. protected head；首次压缩时向 system prompt 追加一条“早期轮次已 compact”的说明。
2. 一个带 handoff 前后界限的 `compaction_summary`。
3. protected tail。
4. 必要时追加 todo snapshot 或真实 user anchor，确保模型请求中仍有真实用户意图。

摘要的 `role` 会依据相邻消息选择，以满足 Provider 的 role alternation；当没有幸存 user
turn 时摘要必须承担 user-role，避免部分 OpenAI-compatible backend 报 “No user query
found”。摘要不是用户真实输入：当前代码用
`_hermes_message_class=context_anchor`、`_hermes_scaffold_kind=compaction_summary` 标记，
并保留 `_compressed_summary` 作为压缩器内部兼容元数据。

最后还会：

- 删除不再匹配的 tool call/result，防止 Provider 拒绝孤立 ID。
- 清理历史图片。
- 清除压缩前消息上的 session-store persistence marker，防止新旧会话 flush 误判并重复写入。
- 记录 message-only 粗略节省量；真正是否低于阈值，要等下一次 Provider `prompt_tokens` 验证。

## 4. `compress_context()`：压缩事务与会话边界

`agent/conversation_compression.py::compress_context()` 是默认引擎之上的事务协调层，负责
把“得到新列表”变成“可恢复的会话状态”。

### 4.1 并发锁和幂等检查

压缩按旧 `session_id` 在 `state.db` 的 `compression_locks` 表上抢租约锁：

- 同一 session 同时只有一个压缩者可以继续；最常见竞争者是正常 turn 和 background review。
- 锁有 TTL，并由后台 refresher 延长；进程崩溃后过期锁可回收。
- 抢锁失败、锁 API 实现报错或 session 所有权检查失败时，安全做法是跳过本轮并原样返回消息。
- 即便已经抢到锁，也会再次检查父 session 是否已被其它路径轮转或原地压缩，防止迟到的旧 Agent 再创建一个 child。
- `finally` 中释放旧 session 上的锁；释放发生在落盘、轮转、memory/context-engine 通知和清理完成之后。

### 4.2 原地压缩（`compression.in_place: true`）

原地模式保持同一个 `session_id`：

1. `archive_and_compact(session_id, compressed)` 在一个数据库操作中把旧 active rows 软归档为 `active=0`。
2. 压缩后的 head/summary/tail 作为新的 `active=1` rows 写回同一个 session。
3. 原始消息仍在磁盘上，可用于搜索/恢复；加载当前上下文时只取 active rows。
4. 不创建 parent/child，不改标题、不迁移 session id、不改变 Gateway 路由。
5. 重置 flush identity 基线，并告诉上层这是“原地边界”；否则下一次 flush 会把已写入的压缩消息重复 append。

这是当前更容易保持一个长期 durable session 的模式，但调用方必须尊重 `active` 过滤和
`_last_compaction_in_place` 信号。

### 4.3 传统会话轮转（`compression.in_place: false`）

轮转模式保留旧会话并创建 continuation：

1. 先尽力把当前 turn 尚未落盘的内容 flush 到旧 session。
2. 旧 session `end_reason='compression'`。
3. 创建新 session id，设置 `parent_session_id=old_session_id`。
4. 迁移 persistent goal，延续标题并按 lineage 自动编号。
5. 更新 ContextVar / 环境变量 / logging session context，确保工具、日志和 DB 使用同一个新 id。
6. 将压缩后的 message list 写入新 session，设置新的 system prompt 和 flush 基线。
7. Gateway 在 Agent 返回后同步 `SessionEntry.session_id` 并保存；否则下一轮仍会读到旧父 session。

如果 child 创建失败，代码会把 Agent id 回滚到仍可索引的父 session，并尝试重新打开父 session；
不能让 Agent 继续使用一个 state.db 中不存在的 orphan id。

### 4.4 边界通知和后处理

成功完成压缩边界后，无论原地还是轮转，都会：

- 调用 context engine 的 `on_session_start(..., boundary_reason="compression")`，让插件延续自己的 DAG/checkpoint 状态。
- 调用 memory manager 的 `on_session_switch(..., reset=False, reason="compression")`，避免 provider 把已被摘要的旧 turn 再累计一次。
- 发出 `session:compress` event，供记忆同步等 hook 处理；原地模式会标记 `in_place=true`。
- 清除文件读取 dedup cache，因为被摘要掉的旧文件内容再次读取时必须拿到真实内容。
- 记录压缩次数、fallback streak 和等待真实 usage 的状态。

以下情况不构成压缩边界，也不应轮转 session：摘要终止失败、并发锁未取得、返回列表为空、
压缩结果与输入语义相同，或原地/轮转的数据库写入未成功。

## 5. 手动 `/compress` 和预览

Gateway 的 `/compress` 使用临时 `AIAgent`，但绑定原始平台、Gateway session key 和
当前 `session_id`，以便插件、memory provider 和会话存储看到正确身份。

支持三种常见形式：

- `/compress`：完整压缩。
- `/compress <focus>`：完整压缩，但摘要优先保留 focus 主题。
- `/compress here [N]`：只压缩较早部分，最近 `N` 个 exchange 原样保留；边界会对齐到 user turn。

`/compress --preview` 只做估算和分段报告，不创建 Agent、不调用摘要模型、不写 DB。
`--aggressive` 在该入口目前不执行无 LLM 的硬删除；如果需要该语义，必须另建完整的
transcript 持久化事务，不能在现有轮转逻辑旁边直接截断列表。

真正执行时的安全顺序是：

```text
读取 canonical transcript
  → 生成压缩结果
  → 先持久化新 transcript
  → 成功后才把 Gateway 活跃指针切到新 session
  → 更新 token 状态并反馈压缩前后估算
```

如果压缩没有发生轮转、也没有完成原地归档，手动入口宁可保留原 transcript，也不调用会
删除所有历史行的全量 rewrite。

## 6. Context Engine 插件边界

插件引擎通过 `context.engine` 显式选择，不能被自动猜测激活。引擎必须提供：

- `name`
- `update_from_response(usage)`
- `should_compress(prompt_tokens)`
- `compress(messages, ...)`

可选提供 `has_content_to_compress()`、`should_compress_preflight()`、生命周期回调和
引擎专属工具。Host 会按函数签名过滤可选参数，因此旧插件即使不接受
`memory_context`/`force` 也能工作；但插件仍必须返回合法的 OpenAI message sequence。

默认 `ContextCompressor` 的 head/tail 切分、摘要格式、tool pair sanitizer 和
`_previous_summary` 都不属于插件的公共契约。新增 Gateway 逻辑应依赖 `ContextEngine`
接口，而不是复制这些私有算法。

## 7. 关键不变量

修改 WebUI、Gateway 或 Agent 接缝时，至少要守住以下不变量：

1. **真实用户任务不丢**：最近真实 user turn 不得只留在“历史摘要”里；必要时插回 user anchor。
2. **工具调用成对**：不能把 assistant tool call 留下而删除对应 tool result，反之亦然。
3. **失败不伪成功**：摘要认证/网络失败时不应把原历史替换成空摘要或无界 fallback。
4. **一次压缩只有一个拥有者**：同一旧 session 不能产生两个并行 child。
5. **落盘后再切指针**：Gateway 的活跃 session 指针只有在新 transcript 可读取后才能更新。
6. **原地压缩不重复 flush**：`archive_and_compact` 后必须重置 flush/identity 基线，不能把压缩结果再 append 一遍。
7. **压缩摘要不是真实 user**：摘要可 durable、可供模型恢复，但 WebUI 展示和 turn 归属应按 `context_anchor/compaction_summary` 过滤。
8. **压缩效果以真实 usage 验证**：message 数变少不等于请求已经低于阈值；下一次 Provider `prompt_tokens` 才是有效性证据。

## 8. 代码定位与回归测试

### 核心代码

| 责任 | 位置 |
| --- | --- |
| 引擎抽象和生命周期 | `agent/context_engine.py` |
| 默认切分、摘要、tool/media 清理 | `agent/context_compressor.py` |
| 压缩事务、锁、摘要后 session 处理 | `agent/conversation_compression.py` |
| turn-start / pre-API 压缩触发 | `agent/turn_context.py`、`agent/conversation_loop.py` |
| Gateway 安全网、会话指针同步 | `gateway/run.py` |
| 手动 `/compress`、preview、partial compress | `gateway/slash_commands.py`、`hermes_cli/partial_compress.py` |
| transcript、active rows、compression lock | `hermes_state.py`、`gateway/session.py` |
| memory provider 压缩前/后 hook | `agent/memory_manager.py` |

### 重点测试

Agent 侧可优先查看：

- `tests/gateway/test_compress_command.py`
- `tests/gateway/test_compress_preview.py`
- `tests/gateway/test_compress_focus.py`
- `tests/gateway/test_compress_plugin_engine.py`
- `tests/gateway/test_compression_in_flight_check.py`
- `tests/gateway/test_compression_failure_session_sync.py`
- `tests/gateway/test_compression_session_id_persistence.py`
- `tests/test_hermes_state_compression_locks.py`

涉及消息语义或 WebUI 展示时，还应结合 WebUI 的
`docs/architecture/HermesAgent消息补充说明文档.md` 和
`docs/plans/压缩摘要上下文锚点修复方案.md`，确认摘要 anchor、真实 user、工具轨迹和
SSE/历史投影没有混淆。

## 9. 排查速查表

| 现象 | 优先检查 |
| --- | --- |
| 每轮都在压缩但 token 仍超阈值 | system prompt/tool schema 的不可压缩下限、真实 `prompt_tokens`、`ineffective_compression_count` |
| 压缩后用户刚才的任务消失 | tail user anchor、`_is_real_user_message()`、是否把摘要误当真实 user |
| 压缩后工具 API 报 call_id 不存在 | `_sanitize_tool_pairs()` 和边界是否切穿 tool call/result group |
| Gateway 仍打开旧会话 | 轮转后 `SessionEntry.session_id` 是否保存、ContextVar 是否与新 id 一致 |
| 压缩结果重复出现两次 | 原地模式的 `conversation_history_after_compression()` 和 flush identity 基线 |
| 摘要模型失败后聊天像卡住 | compression cooldown、`_last_compress_aborted`、辅助模型配置；手动 `/compress` 可强制重试 |
| 两个 child 都在增长 | `compression_locks`、锁 TTL/刷新、旧 parent 已轮转检查 |
| WebUI 出现“Context compaction”用户气泡 | Agent 是否写入 `context_anchor/compaction_summary`，WebUI 是否按该语义过滤，而不是只看 role 或 `_compressed_summary` |
