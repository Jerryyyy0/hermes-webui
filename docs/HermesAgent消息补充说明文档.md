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

## 1. 验证继续类

这类逻辑发生在模型“准备结束本轮”时，但 Agent 判断它还不能安全结束。

### 1.1 verify-on-stop：改了代码但缺少验证证据

实现位置：`hermes-agent/agent/conversation_loop.py`、`hermes-agent/agent/verification_stop.py`

当本轮修改了代码、但没有 fresh passing verification evidence 时，Agent 会：

1. 先保留模型原本准备给出的 assistant 最终回答
2. 再补一条 synthetic `user` nudge
3. 继续下一轮循环，让模型先去验证

特点：

- assistant 候选答案是“真实候选内容”，不是 synthetic
- synthetic 的是后补的 `user` nudge
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

### 1.2 pre_verify hook：插件要求继续验证

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

finalizer 会把 verification continuation 的 synthetic `user` nudge 从 live history 中剔除，只保留真正的 assistant 候选答案。

这意味着：

- `user` nudge 是内部控制消息
- assistant 候选答案是保留的

因此从 Agent 语义上说，verify-on-stop / pre_verify 不是“一对都 synthetic”，而是：

- `user` 是 synthetic
- `assistant` 是真实候选回复

## 2. 空响应恢复类

这类逻辑用于处理“模型刚执行完工具，却没有给出可见文本”的情况。

### 2.1 工具执行后模型返回空内容：补一对 synthetic `assistant + user`

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

`_drop_trailing_empty_response_scaffolding()` 会在持久化前清理 empty-response 相关的内部脚手架，并在需要时把尾部孤立的 tool / assistant(tool_calls) 结构一起回退，避免后续恢复时出现非法 role 序列。

这类逻辑的关键思想是：

- 运行时可以临时注入内部 scaffold
- durable transcript 不能把这些 scaffold 当成真实对话保留下来

## 3. “继续别停”类

这类逻辑用于处理模型“看起来像要结束，但实际上还没完成任务”的场景。

### 3.1 Codex intermediate ack continuation

实现位置：`hermes-agent/agent/conversation_loop.py`

某些 Codex 风格模型会先给一句“我接下来会去做 X”，但还没真的调用工具。Agent 检测到这种中间 ack 后，会：

1. 保留一条 assistant incomplete message
2. 再补一条 `user`：

```text
[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]
```

特点：

- 这条 continue user 消息没有 synthetic flag
- 目前测试明确允许它保留在 `result["messages"]` 中
- 它属于“继续驱动模型的控制消息”，但不是和 verification nudge 同一种“最终必须剔除”的实现

因此它要单独看待，不能简单并入 verification synthetic 策略。

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

## 4. 预算耗尽总结类

### 4.1 max iterations：补一条 `user` 总结请求

实现位置：`hermes-agent/agent/chat_completion_helpers.py`

当 tool-calling iteration 达到上限时，Agent 会额外补一条 `user`：

```text
You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.
```

这条消息的作用是：

- 禁止继续调工具
- 让模型把已有结果总结成最终回答

### 4.2 预算总结 assistant

如果模型成功给出总结，Agent 还会再补一条 assistant 总结消息。

特点：

- 这是一组“要保留”的补消息
- 它们不是内部脚手架 flag 驱动的临时 scaffold
- 但 WebUI 侧可能仍会出于展示语义考虑，额外隐藏某些内部 summary request 形式

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

实现位置：`hermes-agent/agent/conversation_loop.py`

工具被 guardrail 拦截时，Agent 会直接补一条 assistant 解释原因，避免前端看起来像“突然崩了”。

文案信息：

- 没有仓库内置固定全文模板
- 文案来自 `agent._toolguard_controlled_halt_response(decision)`
- 一般是“某个工具因某条 guardrail 规则被拦截”的解释性 assistant 文本
- 因此它属于 **规则驱动的动态 assistant 文案**

### 5.3 本地处理错误 / 接近预算上限错误 assistant

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

### 6.1 todo snapshot synthetic user

实现位置：`hermes-agent/agent/conversation_compression.py`

上下文压缩时，Agent 会把 todo 快照注入成一条 `role="user"` 的 synthetic message。

特点：

- 标记位：`_todo_snapshot_synthetic`
- 它不代表用户真实发言
- 只是为了把待办状态带进压缩后的上下文

文案信息：

- 没有固定字符串
- 内容来自 `agent._todo_store.format_for_injection()`
- 常见形态是待办列表、状态快照、或多行任务摘要
- 因此它属于 **结构固定、文本动态** 的 synthetic user message

### 6.2 压缩后没有真实 user turn：补 user anchor

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

## 7. 还有两类值得单独提到

### 7.1 invalid tool JSON 恢复：补 assistant + tool

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

实现位置：`hermes-agent/agent/moa_loop.py`

Mixture-of-Agents 聚合器在附加 guidance 时，如果最后一条不是 user，会 append 一条 `user` guidance；如果最后一条已经是 user，则直接把 guidance merge 进现有 user turn，避免出现连续 user-user 序列。

这类消息和主聊天 loop 不完全同级，但本质上也是 Agent 主动补 user message 的一种。

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

### 8.1 原则上不应直接当真实聊天内容展示的内部脚手架

- `_empty_recovery_synthetic`
- `_empty_terminal_sentinel`
- `_thinking_prefill`
- `_verification_stop_synthetic`
- `_pre_verify_synthetic`
- `_kanban_stop_synthetic`
- `_todo_snapshot_synthetic`

这些消息的共同点：

- 用于继续循环、修复 role alternation、压缩上下文、或保持协议合法
- 不代表用户真的说了什么，也不代表助手真的交付了什么
- 若直接展示，容易把内部控制平面泄漏到用户 transcript

### 8.2 虽然是 Agent 主动补的，但通常应视为真实 transcript 内容

- interrupt partial assistant
- tool guardrail halt assistant
- 本地处理错误 / runtime 错误 assistant
- finalizer 兜底 assistant
- max-iterations summary request + summary answer
- codex intermediate ack continuation assistant / user

这些消息的共同点：

- 它们承载真实状态解释、真实中断结果、真实结束语义，或当前产品明确允许其进入结果历史

## 9. 一张速查表

| 类别 | 补 `user` | 补 `assistant` | 典型标记 | 默认语义 |
|---|---|---|---|---|
| verify-on-stop | 是 | assistant 候选保留 | `_verification_stop_synthetic` | user 为 synthetic，assistant 为真实候选 |
| pre_verify hook | 是 | assistant 候选保留 | `_pre_verify_synthetic` | user 为 synthetic，assistant 为真实候选 |
| empty response recovery | 是 | 是 | `_empty_recovery_synthetic` | 两边都 synthetic |
| empty terminal sentinel | 否 | 是 | `_empty_terminal_sentinel` | 内部失败哨兵 |
| thinking prefill | 否 | 是 | `_thinking_prefill` | 内部继续驱动 |
| codex ack continue | 是 | 是 | 无专用 synthetic flag | 保留型继续控制消息 |
| kanban stop guard | 是 | 是 | `_kanban_stop_synthetic` | 两边都 synthetic |
| max-iterations summary | 是 | 是 | 无专用 synthetic flag | 保留型总结消息 |
| interrupt / error / finalizer closure | 否 | 是 | 通常无 | 真实 transcript 收尾 |
| compression todo / anchor | 是 | 否 | `_todo_snapshot_synthetic` 等 | 上下文重建 scaffolding |

## 10. 对 WebUI / 展示层的直接启示

如果 WebUI 需要过滤或特殊处理 Hermes Agent 补消息，建议至少按下面的思路做区分：

1. **先按 flag 判断是否为内部 scaffolding**
2. **再按场景判断是否要保留真实 assistant 候选**
3. **不要仅凭 `[System: ...]` 文案字符串判断**

尤其要注意以下差异：

- verification continuation：通常只 synthetic `user`，assistant 候选应保留
- empty recovery：assistant 和 user 两边都是 scaffold
- kanban stop guard：assistant 和 user 两边都是 scaffold
- codex ack continue：虽然是 Agent 补的控制消息，但当前实现并不把它当成 verification-style ephemeral nudge

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
