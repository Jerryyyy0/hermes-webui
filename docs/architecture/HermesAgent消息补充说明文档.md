# Hermes Agent 消息补充说明

本文整理 `hermes-agent` 运行时会主动向会话 `messages` 中补充 `user` / `assistant` message 的主要逻辑，目的是帮助 WebUI 侧区分：

- 哪些消息是模型的正常输出
- 哪些消息是 Agent 为了继续循环、修复序列、压缩上下文、或在异常场景下闭合 transcript 而主动补上的
- 哪些消息属于内部脚手架，原则上不应作为真实聊天内容直接展示

本文只覆盖 **Agent 代码显式追加到 `messages` 的消息**。不讨论模型天然返回的正常回复，也不讨论 WebUI 额外生成的前端展示层消息。

## 总览

Hermes Agent 的“补消息”大致分成六类：

1. 验证继续类
2. 空响应恢复类
3. “继续别停”类
4. 预算耗尽总结类
5. 错误 / 中断 / 收尾闭合类
6. 压缩上下文类

理解这些逻辑时，最重要的是分清两件事：

- **这条消息是不是 synthetic / scaffolding**
- **这条消息最后是否应该进入 durable transcript / WebUI 可见 transcript**

## 术语

### 补消息

指 Agent 运行时通过代码显式 `append(...)` 写入 `messages` 的 message，而不是模型 API 本来返回的内容。

### synthetic / scaffolding

指为了驱动下一轮循环、保持 role alternation、修复非法消息序列、或在压缩上下文时维持结构而临时生成的 message。这些消息通常带有内部标记位，例如：

- `_empty_recovery_synthetic`
- `_empty_terminal_sentinel`
- `_thinking_prefill`
- `_verification_stop_synthetic`
- `_pre_verify_synthetic`
- `_kanban_stop_synthetic`
- `_todo_snapshot_synthetic`

### durable transcript

指最终会被持久化、在后续恢复时继续作为上下文使用的消息历史。某些 synthetic message 只存在于运行时，不应进入 durable transcript。

### 完整轮次示例约定

下文不使用角色编号和内部缩写，而是直接写出用户、助手、工具和内部控制
消息的真实内容。“内部控制”表示 Agent 自动追加的消息，不是用户再次点击
发送。

每个完整示例分别列出 Agent 运行时消息、持久化后的 transcript 和 WebUI
最终展示。三者不一定相同：内部脚手架通常只存在于运行时，context anchor
可以进入持久化历史，但不会显示成用户气泡。

### 流式会话的统一口径

不要把 Agent 内部的 `messages.append({"role": ...})` 误读为 WebUI 会收到一条
同角色的 SSE 消息。`/api/chat/stream` 发出的是运行事件，而不是 transcript 行：

```text
token / reasoning / interim_assistant / tool / tool_complete / done / stream_end
```

因此每个小类都要分四层看：

1. **Agent 内部**：是否实际补了 `user`、`assistant` 或 `tool` 行，供下一次模型调用使用。
2. **SSE 实时**：这条内部行是否会变成 `token` 或 `interim_assistant`；内部 `user` 行没有对应的普通 SSE 事件。
3. **Agent durable**：Agent SQLite 是否保存该行。`internal_scaffold` 不保存；`context_anchor` 可以保存，但带语义标记。
4. **WebUI 收口与历史**：`done.session.messages` 和之后的 `GET /api/session` 都会过滤 `internal_scaffold` 与 `context_anchor`。因此它们不会成为用户气泡。

有一个重要例外：模型的候选 assistant 文本可能已经以 `token` 流出，之后 Agent 才把
对应 assistant 行标成 `internal_scaffold` 并继续循环。此时用户会在实时画面短暂看到候选
文本，但 `done` 和历史只保留后续的最终回答。verify-on-stop、pre_verify、Codex
intermediate ack 和 kanban stop guard 都要按这个例外理解。

## 1. 验证继续类

这类逻辑发生在模型“准备结束本轮”时，但 Agent 判断它还不能安全结束。

### 1.1 verify-on-stop：改了代码但缺少验证证据

实现位置：`hermes-agent/agent/conversation_loop.py`、`hermes-agent/agent/verification_stop.py`

当本轮修改了代码、但没有 fresh passing verification evidence 时，Agent 会：

1. 把模型原本准备给出的 assistant 候选标成 `internal_scaffold`
2. 再补一条同类的 synthetic `user` nudge
3. 继续下一轮循环，让模型先去验证

特点：

- assistant 候选答案在模型语义上是真实候选，但当前实现也带
  `internal_scaffold`，不会写入 durable transcript
- synthetic 的是后补的 `user` nudge；两条消息共享同一 scaffold kind
- 标记位：`_verification_stop_synthetic`

具体文案形态：

```text
[System: You edited code in this turn, but the workspace does not have fresh passing verification evidence yet.

Verification status: <status detail>

Changed paths:
- `<path1>`
- `<path2>`

Run the relevant verification command now (...). If verification is not possible, explain the concrete blocker instead of claiming the work is fully verified.]
```

说明：

- 这是 **固定前缀 + 动态中段** 的模板
- 固定前缀是 `You edited code in this turn...`
- 动态部分主要有三块：`Verification status`、`Changed paths`、命令提示/阻塞说明
- 与之配对的 assistant 不是固定文案，而是“模型原本准备提交的最终回答”，内容完全动态

这类消息的设计意图是：

- 让模型“再跑一轮”
- 不把真正想交付给用户的答案丢掉
- 但不让那条 verification nudge 污染持久化 transcript

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「把登录接口的超时问题修好。」
  助手（内部候选）：「登录接口已修复。」
  用户（内部控制）：「你修改了代码，但还没有新的验证结果。请立即运行相关测试。」
  助手：「测试通过，登录接口的超时问题已修复。」

Agent 持久化后：
  用户：「把登录接口的超时问题修好。」
  助手：「测试通过，登录接口的超时问题已修复。」

WebUI 合并后展示：
  用户：「把登录接口的超时问题修好。」
  助手：「测试通过，登录接口的超时问题已修复。」
```

**流式会话说明**

```text
SSE 实时：
  候选「登录接口已修复」若已从模型流出，会先以 token 出现在当前助手区域。
  Agent 随后把候选和验证 nudge 标成 internal_scaffold；nudge 不会产生 user SSE 事件，
  候选也不会产生普通 interim_assistant 事件。
  验证工具仍照常产生 tool / tool_complete；验证后的回答继续以 token 流出。

done / 历史：
  done 用验证后的完整 session 覆盖临时画面；候选与 nudge 被过滤，只剩最终回答。
```

### 1.2 pre_verify hook：插件要求继续验证

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「把接口改成支持批量上传。」
  助手（内部候选）：「批量上传接口已经完成。」
  用户（插件内部控制）：「请先运行批量上传兼容性检查，再结束本轮。」
  助手：「批量上传已经完成，兼容性检查也通过了。」

Agent 持久化后：
  用户：「把接口改成支持批量上传。」
  助手：「批量上传已经完成，兼容性检查也通过了。」

WebUI 合并后展示：
  用户：「把接口改成支持批量上传。」
  助手：「批量上传已经完成，兼容性检查也通过了。」
```

**流式会话说明**

```text
SSE 实时：
  插件 nudge 没有 user SSE 事件。候选回答如果已经逐字输出，用户可能先看到
  「批量上传接口已经完成」；插件检查和后续最终回答继续以 tool/token 事件呈现。

done / 历史：
  当前 Agent fork 把候选和插件 nudge 都当 internal_scaffold；done 和历史只展示
  「批量上传已经完成，兼容性检查也通过了」。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

这是 verify-on-stop 的并行机制。区别在于它不是内建验证策略，而是来自 `pre_verify` hook / plugin 的继续要求。

特点：

- 同样先保留 assistant 候选答案
- 再补 synthetic `user` nudge
- 标记位：`_pre_verify_synthetic`

具体文案形态：

- 没有仓库内置的一条固定默认文案
- 文案来自 `pre_verify` hook / plugin 的 `get_pre_verify_continue_message(...)`
- 因此它通常是 **插件定义的动态 `user` 控制消息**
- 常见形态仍然是 `[System: ...]` 风格的继续验证提示，但不能假定文本固定

### 1.3 这两类消息的持久化策略

实现位置：`hermes-agent/agent/turn_finalizer.py`

finalizer 会把 verification continuation 的 scaffold 从 live history / durable flush 中剔除；候选 assistant 只作为运行时临时 fallback，不能假定会持久化。

这意味着：

- `user` nudge 是内部控制消息
- assistant 候选答案也是当前实现的内部控制消息；真正最终答案由后续验证轮产生

因此从当前实现说，verify-on-stop / pre_verify 是“一对都带 scaffold”：

- `user` 是 synthetic nudge
- `assistant` 是待验证的临时候选

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「修复配置解析器读取空值时崩溃的问题。」
  助手（内部候选）：「解析器已经修好。」
  用户（内部控制）：「当前没有通过的测试记录，请先验证这次修改。」
  助手：「解析器已修复，相关测试全部通过。」

Agent 持久化后：
  用户：「修复配置解析器读取空值时崩溃的问题。」
  助手：「解析器已修复，相关测试全部通过。」

WebUI 合并后展示：
  用户：「修复配置解析器读取空值时崩溃的问题。」
  助手：「解析器已修复，相关测试全部通过。」
```

**流式会话说明**

```text
这不是第三种 SSE 协议：1.1 和 1.2 都遵循同一规则。
内部候选 / 内部 user nudge 不会成为角色型 SSE 消息；候选 token 可能暂时可见，
真正可持久化、可重载的回答以 done 为准。
```

**实现来源说明**

- 上游原有机制已经会过滤 verification 的 synthetic user nudge，但会保留并
  提前展示待验证的 assistant 候选。
- 当前 Agent fork 的 `93d827f13` 进一步把 assistant 候选也标成
  `internal_scaffold`，因此候选和 nudge 都不持久化，只保存验证后的最终回答。
- WebUI integration 只负责隐藏控制消息和生成可见 transcript，不负责把
  `用户 → 验证后的助手回答` 写入 Agent durable store。

## 2. 空响应恢复类

这类逻辑用于处理“模型刚执行完工具，却没有给出可见文本”的情况。

### 2.1 工具执行后模型返回空内容：补一对 synthetic `assistant + user`

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「查一下今天的错误日志，告诉我最常见的问题。」
  助手：调用日志查询工具。
  工具：「共找到 38 条错误，其中 24 条是连接超时。」
  助手（内部占位）：「(empty)」
  用户（内部控制）：「请处理上面的工具结果并继续完成任务。」
  助手：「今天最常见的是连接超时，共 24 条。」

Agent 持久化后：保留用户请求、工具调用、工具结果和最终回答；删除两条内部消息。

WebUI 合并后展示：
  用户：「查一下今天的错误日志，告诉我最常见的问题。」
  [日志查询工具卡片]
  助手：「今天最常见的是连接超时，共 24 条。」
```

**流式会话说明**

```text
SSE 实时：
  日志工具照常显示为 tool / tool_complete。
  内部「(empty)」assistant 和「请处理工具结果」user 只存在于 Agent messages，
  两者都不会变成 WebUI 的助手/用户气泡。
  下一次模型调用产生的真实结论才以 token 流出。

done / 历史：
  两条 empty-recovery scaffold 被跳过；工具和真实结论收口为正式 transcript。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

当模型在 tool 调用后返回空响应时，Agent 会补：

1. 一个 synthetic `assistant("(empty)")`
2. 一个 synthetic `user("Please process the tool results above and continue...")`

这样做的原因是 role alternation 不能直接从 `tool -> user`，否则很多 provider 会把消息序列判为非法。

特点：

- 这是一对完整的内部脚手架
- assistant 和 user 两边都属于 synthetic
- 标记位：`_empty_recovery_synthetic`

这是最典型的“为了维持协议合法性而成对补消息”的逻辑。

具体文案形态：

assistant：

```text
(empty)
```

user：

```text
You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task.
```

说明：

- 这一类是最容易识别的固定文案对
- assistant 恒为字面量 `(empty)`
- user 是固定英文恢复提示
- 两条都不应被当成真实聊天内容

### 2.2 最终空响应 sentinel：补一个 `assistant("(empty)")`

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「运行健康检查脚本并告诉我结果。」
  助手：调用健康检查工具。
  工具：「检查完成：数据库连接失败。」
  助手（内部失败标记）：「(empty)」

Agent 持久化后：保留可恢复的真实消息和最终错误状态；不保存「(empty)」。

WebUI 合并后展示：
  用户：「运行健康检查脚本并告诉我结果。」
  [健康检查工具卡片：数据库连接失败]
  [本轮无有效回答或错误状态]
```

**流式会话说明**

```text
SSE 实时：
  已发生的工具事件仍可见；内部 terminal 「(empty)」不会额外产生 token 或
  interim_assistant。它不是一条应当展示给用户的助手回答。

done / 历史：
  sentinel 被过滤。终端错误或「未收到响应」由 WebUI 的终态处理表达，不能把
  字面量「(empty)」当成历史中的真实回答。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

如果重试、fallback 等流程都失败，Agent 会在尾部放一个 `assistant("(empty)")` sentinel，标记当前轮以空响应结束。

特点：

- 这是失败哨兵，不是给用户阅读的真实回答
- 标记位：`_empty_terminal_sentinel`

具体文案形态：

```text
(empty)
```

说明：

- 这是字面量固定值，不带额外上下文
- 其“失败解释”通常体现在后续 `final_response`、日志或上层包装，而不是这条 sentinel 本身

### 2.3 thinking-only prefill：只补一条 assistant

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「比较 dev.yaml 和 prod.yaml，告诉我哪个适合生产环境。」
  助手（内部思考预填充）：只有推理内容，没有可展示正文。
  助手：「prod.yaml 更适合生产环境，因为它限制了并发并开启了审计日志。」

Agent 持久化后：只保留用户问题和最终回答，不保存思考预填充。

WebUI 合并后展示：
  用户：「比较 dev.yaml 和 prod.yaml，告诉我哪个适合生产环境。」
  助手：「prod.yaml 更适合生产环境，因为它限制了并发并开启了审计日志。」
```

**流式会话说明**

```text
SSE 实时：
  第一轮通常只有 reasoning（或没有可见事件），因为没有可展示正文。
  thinking-prefill assistant 不发 interim_assistant；第二次模型调用的可见正文才发 token。

done / 历史：
  prefill 不保存、不展示；最终正文经 done 成为唯一助手回答。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

某些模型会返回 structured reasoning，但没有任何可见文本。此时 Agent 会补一条 incomplete assistant，用作“思考预填充”继续下一轮。

特点：

- 只有 assistant，没有 user
- 标记位：`_thinking_prefill`
- 用途是继续驱动模型，不是最终展示内容

具体文案形态：

- 没有固定字符串
- 这条 assistant 的 `content` 通常是模型那一轮已经产出的可见部分，或空的/不完整的 assistant 结构
- 它的关键识别信息不是文案，而是 `_thinking_prefill` 标记

### 2.4 这类消息的清理策略

实现位置：`hermes-agent/run_agent.py`

`_drop_trailing_empty_response_scaffolding()` 会在持久化前清理尾部 empty-response 哨兵并回退孤立的 tool / assistant(tool_calls) 结构；更一般的 scaffold 则由 `internal_scaffold` 语义在 flush 时统一跳过。

这类逻辑的关键思想是：

- 运行时可以临时注入内部 scaffold
- durable transcript 不能把这些 scaffold 当成真实对话保留下来

**完整轮次示例**

```text
Agent 清理前的消息：
  用户：「运行健康检查脚本。」
  助手：调用健康检查工具。
  工具：「检查失败：无法连接数据库。」
  助手（内部占位）：「(empty)」
  用户（内部控制）：「请处理工具结果并继续。」

Agent 持久化后：删除内部占位和内部控制消息；失败的工具尾部按恢复规则回退或闭合。

WebUI 合并后展示：只显示真实用户消息、可展示的工具结果和最终错误状态，不会出现内部占位或催促语。
```

**流式会话说明**

```text
清理动作本身不产生 SSE。它只改变 Agent 送入持久化和 done 合并前的消息集合；
用户已经收到的 tool / token 事件由终态 session 重新收口，内部 pair 不会补显示。
```

### 2.5 输出截断 continuation：保留 partial assistant，再补内部 user

实现位置：`hermes-agent/agent/conversation_loop.py`

模型因输出长度上限或流中断而没有完成回答时，Agent 会先保留已有的 partial
assistant，再补一条 `internal_scaffold` user，要求下一次调用从断点继续。它和
empty-recovery 的差别是：partial assistant 是已经真实产生的回答片段，不是 `(empty)`。

**完整轮次与流式会话示例**

```text
Agent 运行时消息：
  用户：「给我完整列出迁移步骤和回滚方案。」
  助手（partial）：「第一步创建新表并启用双写；第二步……」
  用户（内部控制）：「上一段回答被长度限制截断。请从断点继续，不要重复前文。」
  助手：「第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。」

SSE 实时：
  partial 已经以 token 发出；continuation user 不产生 user SSE。
  后续文本继续以 token 到达，实时 DOM 可能表现为同一段回答的续写。

Agent durable / WebUI done：
  partial assistant 不是 internal_scaffold，可能作为历史片段保留；continuation user 被过滤。
  done 负责把已输出片段与后续结果收口，具体是否折叠为一条展示由 WebUI 投影决定。
```

## 3. “继续别停”类

这类逻辑用于处理模型“看起来像要结束，但实际上还没完成任务”的场景。

### 3.1 Codex intermediate ack continuation

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「检查项目里所有没有处理的 TODO。」
  助手（内部中间回复）：「我接下来会搜索整个项目。」
  用户（内部控制）：「现在立即调用工具，完成任务后再给最终回答。」
  助手：调用项目搜索工具。
  工具：「找到 5 个 TODO，其中 2 个位于发布流程。」
  助手：「共找到 5 个 TODO，其中 2 个会影响发布。」

Agent 持久化后：保留用户请求、工具调用、工具结果和最终回答；删除中间承诺和内部催促语。

WebUI 合并后展示：
  用户：「检查项目里所有没有处理的 TODO。」
  [项目搜索工具卡片]
  助手：「共找到 5 个 TODO，其中 2 个会影响发布。」
```

**流式会话说明**

```text
SSE 实时：
  「我接下来会搜索整个项目」可能先作为 token 出现；它后来被归为 internal_scaffold，
  因而不会再作为普通 interim_assistant 单独发出。
  continue user 不会产生 user SSE 事件；真正搜索过程走 tool 事件，最终结论走 token。

done / 历史：
  中间承诺和 continue user 被过滤，保留工具记录与最终结论。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

某些 Codex 风格模型会先给一句“我接下来会去做 X”，但还没真的调用工具。Agent 检测到这种中间 ack 后，会：

1. 保留一条 assistant incomplete message
2. 再补一条 `user`：

```text
[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]
```

特点：

- 当前实现给 assistant interim 和 continue user 都标记
  `internal_scaffold`（kind=`intent_ack_continuation`）
- 它们只用于继续驱动模型，不应成为最终可见 transcript

因此它要单独看待，但仍属于统一的 scaffold 过滤策略。

文案信息：

user：

```text
[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]
```

assistant：

- 没有固定字符串
- 是模型刚刚输出的那句“中间 ack / 承诺式回复”，例如“我现在去检查目录并总结 3 个重点”
- 这条 assistant 一般会带 `finish_reason="incomplete"`

### 3.2 kanban worker stop guard

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「完成看板里的登录迁移任务，并提交结果。」
  助手（内部候选）：「报告已经写好，我马上结束任务。」
  用户（内部控制）：「任务仍是 running，请调用 kanban_complete 或 kanban_block。」
  助手：调用 kanban_complete，产物为 report.md。
  工具：「任务已标记为完成。」
  助手：「登录迁移任务已完成，结果写在 report.md。」

Agent 持久化后：保留真实请求、看板工具记录和最终回答；删除候选回复及协议提醒。

WebUI 合并后展示：
  用户：「完成看板里的登录迁移任务，并提交结果。」
  [看板任务完成卡片]
  助手：「登录迁移任务已完成，结果写在 report.md。」
```

**流式会话说明**

```text
SSE 实时：
  候选「报告已经写好」可能已经以 token 出现；kanban nudge 不会出现为用户气泡。
  Agent 的状态提示、kanban_complete 工具和最终回答分别走 status/tool/token，不是
  由这对内部 role 消息直接映射出来。

done / 历史：
  kanban_stop 的 assistant / user pair 都是 scaffold；只显示完成工具和最终结论。
```

实现位置：`hermes-agent/agent/conversation_loop.py`、`hermes-agent/agent/kanban_stop.py`

kanban worker 不能只口头说“我接下来去完成”，而必须通过 `kanban_complete` 或 `kanban_block` 结束。否则 Agent 会补：

1. 一条 synthetic assistant
2. 一条 synthetic user nudge

特点：

- 两边都带 `_kanban_stop_synthetic`
- 这是完整的一对内部控制脚手架
- 用来逼 worker 在下一轮立即调用 terminal board tool

和 verification continuation 不同，这里 assistant 也被明确标记成 synthetic。

文案信息：

user：

```text
[System: You are a Hermes kanban worker. A plain-text reply is NOT a terminal state for the board.

Task `<task_id>` is still `running`. Ending now without a board tool causes a protocol violation ...

Do this immediately in your next response — do not narrate intent:
1. Finish any remaining deliverable ...
2. Call `kanban_complete(...)` or `kanban_block(...)` ...

Never end a turn with only a promise of future action. Repeated protocol violations will block this task and require manual intervention.]
```

assistant：

- 没有固定字符串
- 它是模型原本那条“准备结束但还没调用 kanban terminal tool”的回答
- 但在这个场景里，这条 assistant 也会被标成 `_kanban_stop_synthetic`

### 3.3 Codex Responses incomplete continuation：必要时补 assistant 和 user nudge

实现位置：`hermes-agent/agent/conversation_loop.py`

Codex Responses 返回 `finish_reason="incomplete"` 时，Agent 会先保留可重放的
incomplete assistant。若这条 assistant 没有可供 Responses API 重放的内容，还会补一条
`internal_scaffold` user nudge，请模型直接给出最终答案。

**完整轮次与流式会话示例**

```text
Agent 运行时消息：
  用户：「比较两套缓存方案，给出选择建议。」
  助手（incomplete）：「我已比较命中率和失效策略，但还没有写出结论。」
  用户（仅在不可重放时的内部控制）：「请继续，并直接给出最终答案。」
  助手：「建议选方案 B：命中率略低，但失效和回滚更可控。」

SSE 实时：
  incomplete assistant 如果含可见 commentary，会经 interim_assistant 或已发 token 显示。
  内部 nudge 没有 user SSE；后续完整答案继续以 token / done 呈现。

Agent durable / WebUI done：
  incomplete assistant 是否保留取决于它是否是普通可重放行；nudge 是 scaffold，不能显示。
  历史以 done 后的可见 transcript 为准，不能把 nudge 误作用户追问。
```

## 4. 预算耗尽总结类

### 4.1 max iterations：补一条 `user` 总结请求

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「把这个仓库的依赖和测试都检查一遍。」
  助手：多次调用依赖检查和测试工具。
  工具：「测试 126 项，通过 124 项，失败 2 项。」
  用户（内部控制）：「工具调用次数已达到上限，请停止调用工具并总结现有结果。」
  助手：「检查了依赖和测试，目前有 2 个失败项，分别是登录超时和缓存清理。」

Agent 持久化后：保留真实请求、工具记录和总结；不保存内部总结请求。

WebUI 合并后展示：
  用户：「把这个仓库的依赖和测试都检查一遍。」
  [依赖检查和测试工具卡片]
  助手：「目前有 2 个失败项，分别是登录超时和缓存清理。」
```

**流式会话说明**

```text
SSE 实时：
  「已达到工具调用上限，请总结」是内部 user scaffold，没有 user SSE 事件。
  对普通 Chat Completions / Anthropic summary 路径，汇总调用是非流式的，通常直到 done
  才出现总结；Codex Responses summary 路径可继续发 token。

done / 历史：
  summary request 被过滤，真正的 summary assistant 保留。
```

实现位置：`hermes-agent/agent/chat_completion_helpers.py`

当 tool-calling iteration 达到上限时，Agent 会额外补一条 `user`：

```text
You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.
```

这条消息的作用是：

- 禁止继续调工具
- 让模型把已有结果总结成最终回答

### 4.2 预算总结 assistant

**完整轮次示例**

```text
Agent 运行时消息（成功）：
  用户：「检查依赖和测试，最后给我一份结论。」
  用户（内部控制）：「已达到工具调用上限，请总结已有结果。」
  助手：「依赖没有安全漏洞；测试有 2 项失败，需要修复登录超时和缓存清理。」

Agent 持久化后：保存用户原始请求和助手总结，不保存内部总结请求。

WebUI 合并后展示：
  用户：「检查依赖和测试，最后给我一份结论。」
  助手：「依赖没有安全漏洞；测试有 2 项失败，需要修复登录超时和缓存清理。」

如果总结失败，WebUI 展示明确的失败说明，而不是把内部总结请求显示成用户消息。
```

**流式会话说明**

```text
这一小类的 assistant summary 不是 scaffold，而是最终交付。它是否逐字显示取决于
provider：Codex Responses 可流式；其它 summary 分支可能只在 done 一次性出现。
无论哪种，内部总结请求永远不会显示成用户的第二句话。
```

如果模型成功给出总结，Agent 还会再补一条 assistant 总结消息。

特点：

- assistant 总结是要保留的真实交付
- summary request 当前带 `internal_scaffold` kind=`max_iteration_summary_request`，不应保留为 user 气泡

文案信息：

- `user` 侧是固定总结请求
- `assistant` 侧没有固定模板，它是模型基于当前上下文生成的自然语言总结
- 如果总结调用失败，还可能出现这些 fallback 文案：

```text
I reached the iteration limit and couldn't generate a summary.
I reached the maximum iterations (<n>) but couldn't summarize. Error: <error>
```

## 5. 错误 / 中断 / 收尾闭合类

这类逻辑的核心目标不是继续驱动模型，而是：

- 把已经对用户可见的内容写回 transcript
- 或保证 transcript 尾部以 assistant 收口，避免恢复时序列不合法

### 5.1 中断时保留 partial assistant

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「生成一份完整的数据库迁移方案。」
  助手：「迁移分为准备、双写、切流和回滚四个阶段。准备阶段需要……」
  用户点击“停止”，输出在这里中断。

Agent 持久化后：保存用户请求和已经输出的半段助手正文；不会补一条假的用户消息。

WebUI 合并后展示：
  用户：「生成一份完整的数据库迁移方案。」
  助手：「迁移分为准备、双写、切流和回滚四个阶段。准备阶段需要……」[已中断]
```

**流式会话说明**

```text
SSE 实时：
  这段 partial 本来已经由 token 发出；用户点击停止后，服务端停止后续普通事件。
  Agent 是把已显示的 token 回补为 assistant durable 行，并没有自动补一条 user 消息。

done / 历史：
  中断路径可能没有正常 done，而是靠取消/恢复持久化显示 partial；刷新历史仍应看到
  已经发出的部分，而不是重新伪造一条完整回答。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

如果用户 stop 时已经流出一部分 assistant 文本，Agent 会把这段 partial text append 回 `messages`。

意义：

- 用户已经看见了这段输出
- transcript 不能装作它从未发生

文案信息：

- 如果已经流出了可见文本，则直接保留那段 partial assistant 文本，本身没有统一模板
- 如果一丁点可见文本都没有，则会走固定前缀模板：

```text
Operation interrupted: waiting for model response (<seconds>s elapsed).
```

### 5.2 tool guardrail halt assistant

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「删除生产环境里 30 天前的订单数据。」
  助手：尝试调用数据删除工具。
  工具：「操作被安全规则拦截：缺少生产删除授权。」
  助手：「我没有删除任何数据。生产环境删除需要额外授权。」

Agent 持久化后：保存用户请求、被拦截的工具记录和助手说明。

WebUI 合并后展示：
  用户：「删除生产环境里 30 天前的订单数据。」
  [删除工具卡片：已拦截]
  助手：「我没有删除任何数据。生产环境删除需要额外授权。」
```

**流式会话说明**

```text
SSE 实时：
  被拦截的调用先以 tool / tool_complete 呈现。guardrail assistant 是 Agent 本地生成的
  final_response，不是一次新的模型流；通常由 done 收口为最终助手消息，而不是 token。

done / 历史：
  这是有意交付给用户的真实 assistant 说明，会被保留。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

工具被 guardrail 拦截时，Agent 会直接补一条 assistant 解释原因，避免前端看起来像“突然崩了”。

文案信息：

- 没有仓库内置固定全文模板
- 文案来自 `agent._toolguard_controlled_halt_response(decision)`
- 一般是“某个工具因某条 guardrail 规则被拦截”的解释性 assistant 文本
- 因此它属于 **规则驱动的动态 assistant 文案**

### 5.3 本地处理错误 / 接近预算上限错误 assistant

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「读取 customer-data.csv 并总结异常数据。」
  助手：调用文件读取工具。
  工具处理连续失败。
  助手：「处理文件时发生错误：CSV 第 18 行字段数量不一致。」

Agent 持久化后：保存用户请求和助手错误说明，不补任何内部用户消息。

WebUI 合并后展示：
  用户：「读取 customer-data.csv 并总结异常数据。」
  助手：「处理文件时发生错误：CSV 第 18 行字段数量不一致。」
```

**流式会话说明**

```text
SSE 实时：
  已完成的工具仍有 tool 事件；错误 assistant 是 Agent 本地补写的终态说明，通常通过
  done 出现，而不是模型再流一遍 token。没有内部 user nudge。

done / 历史：
  该 assistant 用来闭合真实失败结果，保留为正式 transcript。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

当出现本地处理错误，或在预算边缘出现反复错误时，Agent 会补一条 assistant 错误说明。

这样做是为了：

- 不让 transcript 尾部停在 `user` 或 `tool`
- 保证下一轮恢复时上下文闭合

文案信息：

本地处理错误：

```text
I apologize, but I encountered an error while processing the model response: <error_msg>
```

接近预算上限时反复错误：

```text
I apologize, but I encountered repeated errors: <error_msg>
```

### 5.4 runtime context 不足 assistant

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「分析整个项目，找出登录链路的性能瓶颈。」
  模型服务：「当前上下文只有 8192 tokens，低于可靠工具调用所需容量。」
  助手：「当前模型上下文不足。请把 Ollama 的上下文提高到 65536 后重新加载模型。」

Agent 持久化后：保存用户请求和上下文不足的助手说明。

WebUI 合并后展示：
  用户：「分析整个项目，找出登录链路的性能瓶颈。」
  助手：「当前模型上下文不足。请把 Ollama 的上下文提高到 65536 后重新加载模型。」
```

**流式会话说明**

```text
SSE 实时：
  运行时上下文检查在模型正常输出前失败，因此没有候补 user，也通常没有 assistant token。
  Agent 生成的上下文不足说明在终态作为 assistant 出现。

done / 历史：
  说明是面向用户的真实失败结果，会被持久化和重载。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

例如 Ollama runtime context 太小，Agent 会直接补 assistant 错误消息说明上下文不足。

文案信息：

- 这是 **固定前缀 + 动态参数** 的错误模板
- 典型形态是：

```text
Ollama loaded `<model>` with only <runtime_ctx> tokens of runtime context, but Hermes needs at least <minimum_ctx> tokens for reliable tool use.

Increase the Ollama context for this model and restart/reload the model before trying again. A known-good starting point is 65,536 tokens. In Hermes config, set `model.ollama_num_ctx: 65536` ...
```

- 其中模型名和当前 runtime context 是动态的，后面的修复建议正文基本固定

### 5.5 finalizer 统一兜底：`final_response => assistant row`

**完整轮次示例**

```text
Agent 收尾前的消息：
  用户：「运行发布前检查并告诉我结果。」
  助手：调用发布检查工具。
  工具：「12 项检查全部通过。」
  当前 final_response：「发布前检查完成，12 项全部通过。」

finalizer 持久化后：在工具结果后补写一次助手最终回答。

WebUI 合并后展示：
  用户：「运行发布前检查并告诉我结果。」
  [发布检查工具卡片]
  助手：「发布前检查完成，12 项全部通过。」

最终回答只显示一次。
```

**流式会话说明**

```text
finalizer 不会重新向 SSE 发 token。它只保证 Agent messages 中已有 final_response 对应一条
assistant durable 行；WebUI 在 done 拿到这条行后显示它。若文本此前已流出，前端的
response_previewed / done 收口逻辑避免再显示一遍。
```

实现位置：`hermes-agent/agent/turn_finalizer.py`

这是最关键的统一收尾规则：

- 只要当前轮已经有 `final_response`
- 但 transcript 尾部没有 assistant row
- finalizer 就会补一条 assistant，把这轮闭合

这是 Hermes Agent 消息补充逻辑里最重要的 invariant 之一：

> delivered final_response => transcript ends with assistant answer

它的作用是避免：

- durable transcript 尾部停在 `tool`
- 下一轮恢复后模型误以为上一个 user 还没被回答

文案信息：

- finalizer 自己没有固定文案模板
- 它补上的 assistant 内容，就是当前已有的 `final_response`
- 因此这里的关键不是“某句固定文本”，而是“把已有最终回答补写入 transcript”

## 6. 压缩上下文类

这类逻辑和当前轮交付关系不大，而是服务于压缩后的上下文重建。

### 6.1 todo snapshot：可持久化的 context-anchor user

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「先修复登录超时，再补回归测试。」
  助手：「我先处理登录超时。」
  用户（隐藏的待办快照）：「待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。」
  助手：「登录超时已修复，现在开始补回归测试。」

Agent 持久化后：待办快照作为 context anchor 保存，供后续模型继续使用。

WebUI 合并后展示：
  用户：「先修复登录超时，再补回归测试。」
  助手：「我先处理登录超时。」
  助手：「登录超时已修复，现在开始补回归测试。」

待办快照不会显示成用户新发的一句话。
```

**流式会话说明**

```text
SSE 实时：
  todo snapshot 是压缩/上下文重建数据，不会产生 user SSE 事件，也不是实时聊天气泡。
  本轮真正的工具、thinking 和正文仍按各自 SSE 事件发送。

Agent durable：
  context_anchor 可以写入 Agent state.db，并保留 hermes_message_class / scaffold_kind。

done / 历史：
  WebUI projection 过滤 context_anchor；用户只看到真实请求和真实回答。
```

实现位置：`hermes-agent/agent/conversation_compression.py`

上下文压缩时，Agent 会把 todo 快照注入成一条 `role="user"` 的 model-only context anchor。

特点：

- 当前权威标记：`_hermes_message_class="context_anchor"`、kind=`todo_snapshot`
- `_todo_snapshot_synthetic` 仅是兼容旧消费者的 legacy flag
- 它不代表用户真实发言
- 只是为了把待办状态带进压缩后的上下文

文案信息：

- 没有固定字符串
- 内容来自 `agent._todo_store.format_for_injection()`
- 常见形态是待办列表、状态快照、或多行任务摘要
- 因此它属于 **结构固定、文本动态、可持久化但不可见** 的 context-anchor user message

### 6.2 压缩后没有真实 user turn：补 user anchor

**完整轮次示例**

```text
压缩前的真实对话：
  用户：「继续分析刚才的登录性能问题。」
  助手：调用性能分析工具。
  工具：「数据库查询占总耗时的 72%。」

Agent 压缩后的消息：
  系统摘要：「用户正在分析登录性能，数据库查询占总耗时的 72%。」
  用户（隐藏锚点）：「请根据上面的压缩上下文继续。当前没有可复用的真实用户轮次。」
  助手：「主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。」

Agent 持久化后：隐藏锚点作为 context anchor 保存。

WebUI 合并后展示：保留原有真实对话，并显示助手的性能结论；隐藏锚点不会形成用户气泡。
```

**流式会话说明**

```text
这是压缩完成后为下一次模型调用准备的 user-role anchor，不是当前 SSE 中新来的用户输入。
它不会发 user SSE；即使被 Agent 持久化，done 和重新加载历史都会按 context_anchor 过滤。
```

实现位置：`hermes-agent/agent/conversation_compression.py`

如果压缩结果中没有真实 human user turn，Agent 会尽量把最近的一条真实 user 问题重新插入压缩结果；如果连这个都没有，就补一个 fallback user marker。

典型 fallback 文案是：

```text
Continue from the compressed conversation context above. This marker exists because no human user turn was available.
```

这类消息的目的不是给用户看，而是维持压缩后上下文对后续模型调用仍然有可行动的人类锚点。

### 6.3 哪些 `role="user"` 其实不算真实用户输入

实现位置：`hermes-agent/agent/conversation_compression.py`

压缩逻辑明确区分“真实 user turn”和“仅仅是 user-role scaffolding”。以下几类会被排除出真实 human intent：

- `_todo_snapshot_synthetic`
- `_empty_recovery_synthetic`
- `_verification_stop_synthetic`
- `_pre_verify_synthetic`
- context summary / synthetic prefix 形式的 user-role scaffold

这说明在 Hermes Agent 的语义里：

- `role="user"` 不等于“就是人类输入”
- 判断是否为真实用户意图，必须结合 flag 和内容语义

**完整轮次示例**

```text
Agent 持久化消息中同时存在：
  用户（context anchor）：「待办：修复登录，然后补测试。」
  用户（真实输入）：「继续修复登录，先不要处理测试。」
  助手：「好的，我先只处理登录问题。」

WebUI 合并后展示：
  用户：「继续修复登录，先不要处理测试。」
  助手：「好的，我先只处理登录问题。」

虽然两条消息的 role 都是 user，但待办快照不会被显示，也不会被当成一次真实用户轮次。
```

**流式会话说明**

```text
从 SSE 看，只有浏览器提交的真实用户输入会启动这条 stream；context-anchor user 不会在
流中制造第二个用户气泡。它只影响 Agent 后续请求的模型上下文和压缩恢复。
```

## 7. 还有两类值得单独提到

### 7.1 invalid tool JSON 恢复：补 assistant + tool

**完整轮次示例**

```text
Agent 运行时消息：
  用户：「检查 staging 环境能不能部署。」
  助手：调用部署检查工具，参数写成了无效 JSON。
  工具：「参数格式错误。请传入 {\"environment\": \"staging\"}。」
  助手：使用正确参数重新调用部署检查工具。
  工具：「检查通过，可以部署。」
  助手：「staging 环境部署检查通过，可以部署。」

Agent 持久化后：保存两次工具调用、错误结果、成功结果和最终回答。

WebUI 合并后展示：
  用户：「检查 staging 环境能不能部署。」
  [部署检查工具卡片：第一次参数错误，第二次成功]
  助手：「staging 环境部署检查通过，可以部署。」
```

**流式会话说明**

```text
SSE 实时：
  原始 assistant 的 tool call 通过 tool 事件表现，坏 JSON 的说明是 tool result，不是 user nudge。
  因此用户看到的是失败工具卡片、重试工具卡片和后续 token，而不是 Agent 自己补的一条用户消息。

done / 历史：
  原始 assistant(tool_calls)、工具错误/成功结果和最终回答都是可恢复业务轨迹，按工具语义
  展示；这里没有 internal_scaffold assistant + user pair。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

当模型生成了坏掉的 tool call JSON，超过重试次数后，Agent 不会补 user，而是：

1. 先补一条带坏 tool_calls 的 assistant
2. 再补若干 tool error result

这是为了让模型下一轮直接基于工具错误自我修复，而不是把恢复指令伪装成用户消息。

文案信息：

assistant：

- 没有固定字符串
- 是模型原始那条带坏 `tool_calls` 的 assistant message

tool result：

```text
Error: Invalid JSON arguments. <error>. For tools with no required parameters, use an empty object: {}. Please retry with valid JSON.
```

或：

```text
Skipped: other tool call in this response had invalid JSON.
```

### 7.2 MoA aggregator guidance：必要时补一条 user

**完整轮次示例**

```text
用户真实输入：
  用户：「综合几个模型的意见，告诉我数据库迁移该选方案 A 还是方案 B。」

MoA 聚合器收到的消息：
  用户（隐藏指导）：「参考模型 1 认为 A 风险低；参考模型 2 认为 B 停机时间短；请综合这些意见。」
  助手：「建议选择方案 B，但需要先补充回滚演练来控制风险。」

Agent 持久化后：隐藏指导不写入主会话，只保留正常的用户问题和聚合答案。

WebUI 合并后展示：
  用户：「综合几个模型的意见，告诉我数据库迁移该选方案 A 还是方案 B。」
  助手：「建议选择方案 B，但需要先补充回滚演练来控制风险。」
```

**流式会话说明**

```text
MoA guidance 发生在聚合器自己的模型输入中，不是主 WebUI chat stream 的 transcript 行。
它不会向当前浏览器发送 user SSE，也不会作为主会话 durable/history 的用户消息；主会话
只接收聚合后的最终助手结果（若该调用走流式，则是 token，随后 done）。
```

实现位置：`hermes-agent/agent/moa_loop.py`

Mixture-of-Agents 聚合器附加 guidance 时，如果最后一条不是 user，会 append
一条 `user` guidance。如果最后一条已经是 user，则直接把 guidance merge 进
现有 user turn，避免出现连续 user-user 序列。

这类消息和主聊天 loop 不完全同级，但本质上也是 Agent 主动补 user message 的一种。

## Provider 请求合并与 durable transcript

当严格 provider 不能接受连续 `user` 角色时，Agent 可以在**发送给模型的
`api_messages` 副本**中临时合并相邻 user 文本。该副本不写入 Agent
`state.db`、WebUI sidecar 或普通 SSE user 事件；canonical transcript 必须保留
每条真实 user 提交及其独立 turn key。不得在 canonical `messages` 上原地合并，
否则 workspace、附件和下一次真实请求会被持久化成一条伪造的 user 消息。

文案信息：

- guidance 文本不是固定一句
- 常见前缀是：

```text
[Mixture of Agents context — use this as private guidance for the normal Hermes agent loop. You may call tools, continue reasoning, or finish normally.]
Aggregator: <label>
References: <labels>
```

- 后面会拼接 reference outputs 的综合摘要，因此整体属于 **固定头 + 动态正文**

## 8. 可见性与语义分类

从 WebUI 或下游消费方视角，可以把这些补消息粗分为两大类。

### 8.1 原则上不应直接当真实聊天内容展示的控制消息

- `_empty_recovery_synthetic`
- `_empty_terminal_sentinel`
- `_thinking_prefill`
- `_verification_stop_synthetic`
- `_pre_verify_synthetic`
- `_kanban_stop_synthetic`
- `internal_scaffold` kind=`length_continuation`
- `internal_scaffold` kind=`codex_incomplete_nudge`
- `context_anchor`（包括 legacy `_todo_snapshot_synthetic`）

这些消息的共同点：

- 用于继续循环、修复 role alternation、压缩上下文、或保持协议合法
- 不代表用户真的说了什么，也不代表助手真的交付了什么
- 若直接展示，容易把内部控制平面泄漏到用户 transcript

**流式归类**

这些行不会产生普通的 `user` SSE。若其配对 assistant 是在标记前已流出的模型文本，
实时 token 仍可能短暂存在；最终是否保留必须以 `done` 的投影为准。

### 8.2 虽然是 Agent 主动补的，但通常应视为真实 transcript 内容

- interrupt partial assistant
- tool guardrail halt assistant
- 本地处理错误 / runtime 错误 assistant
- finalizer 兜底 assistant
- max-iterations summary answer（summary request 本身仍是内部 scaffold）
- output-length continuation 中已经真实输出的 partial assistant

Codex intermediate ack continuation 的 assistant / user 都带
`internal_scaffold`，应按控制消息处理，不应直接当作真实 transcript 展示。

这些消息的共同点：

- 它们承载真实状态解释、真实中断结果、真实结束语义，或当前产品明确允许其进入结果历史

**流式归类**

partial assistant 是先通过 token 显示、再写回 durable；本地错误、guardrail 和 finalizer
闭合则可能没有 token，只在 `done` 的最终 session 中出现。这两种都不是内部 user 气泡。

## 9. 一张速查表

| 类别 | 补 `user` | 补 `assistant` | SSE 实时会发生什么 | durable / 历史 |
|---|---|---|---|---|
| verify-on-stop | 是 | 临时候选 | 候选 token 可能先出现；nudge 不发 user SSE | 两边 scaffold 被过滤，只留验证后回答 |
| pre_verify hook | 是 | 临时候选 | 与 verify-on-stop 相同，检查过程可见 | 两边 scaffold 被过滤，只留验证后回答 |
| empty response recovery | 是 | 是 | 工具可见；内部 `(empty) + user` pair 不发角色事件 | pair 不保存、不显示 |
| empty terminal sentinel | 否 | 是 | 不发 token/interim；由终态失败处理表达 | sentinel 不显示 |
| thinking prefill | 否 | 是 | 可能只有 reasoning；预填充不发 interim | 只留后来真实正文 |
| length continuation | 是 | 已输出的 partial | partial 已是 token；内部 continuation user 不发 SSE | partial 可保留，user 被过滤 |
| Codex ack continue | 是 | 是 | 中间 ack token 可能先出现；nudge 不发 user SSE | pair 被过滤，保留工具和最终回答 |
| kanban stop guard | 是 | 是 | 候选 token 可能先出现；状态/工具另行发事件 | pair 被过滤，保留看板终态和最终回答 |
| Codex incomplete continuation | 必要时 | incomplete assistant | 可见 incomplete 可实时显示；nudge 不发 user SSE | nudge 过滤，assistant 视可重放状态处理 |
| max-iterations summary | 是 | 是 | request 无 user SSE；Codex 可流式，其它 summary 分支通常等 done | 只保留真实 summary assistant |
| interrupt / error / finalizer closure | 否 | 是 | partial 是已发 token；本地闭合回答通常由 done 出现 | 真实 transcript 收尾 |
| compression todo / anchor | 是 | 否 | 不产生 user SSE，不是实时聊天输入 | 可存 state.db，但 WebUI 投影过滤 |
| invalid tool JSON | 否 | 是（带 tool call） | 失败/重试通过 tool 事件呈现 | 保留可恢复工具轨迹 |
| MoA guidance | 聚合器内部 | 否 | 不属于主 chat SSE | 不进入主会话 transcript |

## 10. 对 WebUI / 展示层的直接启示

如果 WebUI 需要过滤或特殊处理 Hermes Agent 补消息，建议至少按下面的思路做区分：

1. **先按 flag 判断是否为内部 scaffolding**
2. **再按场景判断是否要保留真实 assistant 候选**
3. **不要仅凭 `[System: ...]` 文案字符串判断**

尤其要注意以下差异：

- verification continuation：当前实现 assistant 候选和 user nudge 都是 scaffold；后续最终答案才保留
- verification / ack / kanban 的候选正文若已经走过 `token`，实时画面可能短暂显示；这不是
  一条独立的 `interim_assistant` SSE，也不代表它会进入 done 或历史
- empty recovery：assistant 和 user 两边都是 scaffold
- kanban stop guard：assistant 和 user 两边都是 scaffold
- codex ack continue：当前实现使用 `internal_scaffold`，不能按普通 user/assistant 气泡展示
- max-iterations summary：内部 user request 不显示，而最终 summary 是否逐字流出取决于所走的
  provider 分支，不能假定所有模型都在这一步继续 token streaming

因此，“所有补出来的 user / assistant 都统一过滤”是错误的；“所有 `[System: ...]` 都统一显示”也同样错误。

## 11. 结论

Hermes Agent 的消息补充逻辑，本质上是在维护三个目标：

1. **继续驱动模型完成未完成的工作**
2. **修复或维持合法的消息序列**
3. **保证 durable transcript 与用户实际看到的完成状态一致**

因此，`messages` 并不只是“用户输入 + 模型输出”的朴素流水，而是包含了一层 Agent 运行时控制平面。

要正确消费这些消息，无论是在 WebUI、Gateway、导出器，还是后续 resume / compression / replay 逻辑中，都需要把以下三类区分清楚：

- 真实 human user turn
- 真实 assistant deliverable
- runtime scaffolding / synthetic control message

只有把这三类分开，才能既不泄漏内部控制消息，也不错误丢失真实答案。
