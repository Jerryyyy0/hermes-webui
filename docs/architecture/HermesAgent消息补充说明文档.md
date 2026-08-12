# Hermes Agent 消息补充说明

本文整理 `hermes-agent` 运行时会主动向会话 `messages` 中补写或修复 message 的主要逻辑。
内容以 `user` / `assistant` 为主，必要时包含配套的 `tool` result，目的是帮助 WebUI 侧区分：

- 哪些消息是模型的正常输出
- 哪些消息是 Agent 为了继续循环、修复序列、压缩上下文、或在异常场景下闭合 transcript 而主动补上的
- 哪些消息属于内部脚手架，原则上不应作为真实聊天内容直接展示

本文只覆盖 **Agent 代码显式追加、插入或修复到 `messages` 的消息**。
不讨论模型天然返回的正常回复，也不讨论 WebUI 额外生成的前端展示层消息。
异步后台子任务是唯一需要补充说明的跨层例外：WebUI scheduler 负责唤醒，Agent 随后把
完成通知作为当前调用的控制 user 写入 `messages`；它不是前端凭空生成的展示消息。

下文“机制启用状态”按当前 Agent 的默认配置和运行时 gate 描述：
“默认具备”表示代码路径可用，不代表每轮都会触发；“条件启用”表示还要满足
模型、工作面、配置或异常状态等条件；本地 `config.yaml` 的显式配置可以覆盖默认值。

本文以 Hermes Agent 上游契约作为 canonical / durable 基线，并额外定义 WebUI 的
一问一答展示契约。若本地 fork 尚未符合目标，会在对应小节标成“待修复”，
不把目标状态误写成当前已实现状态。

## 总览

Hermes Agent 的“补消息”大致分成六类：

1. 验证继续类
2. 空响应恢复类
3. “继续别停”类
4. 预算耗尽总结类
5. 错误 / 中断 / 收尾闭合类
6. 压缩上下文类

其中第 3 类还包含一条容易漏记的跨轮机制：`delegate_task` 将子任务放到后台执行，
子任务完成后由 WebUI 在会话空闲时启动一次专用唤醒轮次。它不是普通的“补一条 user”，
而是“后台完成事件 → 隐藏 context anchor → 原用户轮次归属的 assistant 结果”。

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
- 带 `_verification_stop_synthetic` 的 verification user nudge
- 带 `_pre_verify_synthetic` 的 pre_verify user nudge
- `_kanban_stop_synthetic`
- `_todo_snapshot_synthetic`

### durable transcript

指最终会被持久化、在后续恢复时继续作为上下文使用的消息历史。某些 synthetic message 只存在于运行时，不应进入 durable transcript。

### context anchor

指需要写入 durable transcript、供模型恢复或继续执行，但不代表真实用户发言或
最终助手回答的上下文材料。WebUI 应隐藏它，Agent 不应像处理
`internal_scaffold` 一样从持久化中删除它。

### 一问一答展示

指 WebUI 将一个真实 user turn 收口为一个最终 assistant 回答区域。Agent durable
可以保留候选、partial、恢复提示和工具轨迹；WebUI 通过投影隐藏控制行、合并续写，
而不是反向修改 Agent canonical transcript。

### 四层示例约定

下文每个小类都按同一条因果链写示例，而不是把“持久化”和“WebUI 合并”混为
一个动作：

1. **Agent 运行时消息**：接近实际 API 交互的 `messages` 顺序，含真实 user、
   assistant、tool 以及 Agent 补写的控制行。
2. **Agent 持久化后**：最终写入 SQLite / JSON session log 的 durable transcript。
   运行时内部 scaffold 若被过滤或尾部清理，不会先写入再删除数据库记录。
3. **WebUI 展示投影**：只读取 durable transcript 并按语义隐藏控制行、渲染工具、
   替换候选或拼接 partial；这不回写 Agent canonical transcript。
4. **SSE 实时与 `done`**：说明哪些是可见事件，哪些只存在于 Agent 内存或模型输入。

“内部控制”表示 Agent 自动追加的消息，不是用户再次点击发送。若小类只发生在
上游、provider 请求副本或分类汇总中，示例会明确标出该范围，不能虚构成当前 Fork
已有的一次主会话持久化。

### 持久化与展示投影不是同一件事

`internal_scaffold` 的过滤发生在 Agent durable 写入边界；`context_anchor` 则保留在
durable transcript，但由 WebUI 的展示投影隐藏。工具轨迹仍是 durable 行，WebUI 把它
渲染为工具卡片。候选替换或 length partial 拼接只改变当前回答区域，不会删除或合并
Agent 已持久化的真实 assistant 行。

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

模型准备结束一轮时产生的 assistant 候选不能仅凭“后面又补了 user nudge”就统一
归为 `internal_scaffold`。真实候选和 partial 按上游语义持久化，WebUI 再在同一
回答区域内更新、替换或合并。只有上游本来就不持久化的临时行才使用
`internal_scaffold`。

需要保留给模型、但不应显示成用户气泡的控制提示，目标语义是
`context_anchor`。不得为了实现一问一答而删除 durable 内容、修改
`repair_message_sequence`，或只按 `finish_reason="stop"` 过滤回答。

## 1. 验证继续类

这类逻辑发生在模型“准备结束本轮”时，但 Agent 判断它还不能安全结束。

### 1.1 verify-on-stop：改了代码但缺少验证证据

**机制启用状态：⚠️ 条件启用（默认 `auto`）。** 交互式编码或程序化工作面中，本轮修改代码且没有新的通过验证证据时触发；显式 `false` 或消息类工作面不触发。环境变量 `HERMES_VERIFY_ON_STOP` 优先于 `agent.verify_on_stop`；已迁移配置可能已被写成 `false`。

**WebUI 对齐状态：✅ 已对齐。** 控制 nudge 被隐藏；候选与后续最终回答收口到同一回答区域，并保留候选兜底。

**上游持久化规则：✅ 一致。** assistant 候选保留；只有 synthetic user nudge 不进入 durable transcript。Fork 仅补充语义标记。

实现位置：`hermes-agent/agent/conversation_loop.py`、`hermes-agent/agent/verification_stop.py`

当本轮修改了代码、但没有 fresh passing verification evidence 时，Agent 会：

1. 保留模型原本准备给出的 assistant 候选，作为真实 assistant 内容提前展示并持久化
2. 再补一条带 `internal_scaffold` 的 synthetic `user` nudge
3. 继续下一轮循环，让模型先去验证

特点：

- assistant 候选答案是真实内容，不带 `internal_scaffold`，会写入 durable transcript
- synthetic 的只有后补的 `user` nudge
- user nudge 的标记位：`_verification_stop_synthetic`，统一语义为
  `class=internal_scaffold`、`kind=verification_stop`

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

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：候选和 synthetic nudge 都暂时存在。

```json
[
  {
    "role": "user",
    "content": "把登录接口的超时问题修好。",
    "_turn_key": "turn:1"
  },
  {
    "role": "assistant",
    "content": "登录接口已修复。",
    "finish_reason": "verification_required"
  },
  {
    "role": "user",
    "content": "你修改了代码，但还没有新的验证结果。请立即运行相关测试。",
    "_hermes_message_class": "internal_scaffold",
    "_hermes_scaffold_kind": "verification_stop",
    "_verification_stop_synthetic": true
  },
  {
    "role": "assistant",
    "content": "测试通过，登录接口的超时问题已修复。",
    "finish_reason": "stop"
  }
]
```

Agent durable transcript：只过滤 synthetic user，候选 assistant 仍然保留。

```json
[
  {
    "role": "user",
    "content": "把登录接口的超时问题修好。",
    "_turn_key": "turn:1"
  },
  {
    "role": "assistant",
    "content": "登录接口已修复。",
    "finish_reason": "verification_required"
  },
  {
    "role": "assistant",
    "content": "测试通过，登录接口的超时问题已修复。",
    "finish_reason": "stop"
  }
]
```

WebUI 展示投影：不是回写或合并 durable transcript，只隐藏候选。

```json
[
  {
    "role": "user",
    "content": "把登录接口的超时问题修好。",
    "_turn_key": "turn:1"
  },
  {
    "role": "assistant",
    "content": "测试通过，登录接口的超时问题已修复。",
    "finish_reason": "stop"
  }
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  候选「登录接口已修复」若已从模型流出，会先以 token 出现在当前助手区域；
  Agent 仍通过 interim assistant callback 结算该候选，并用 already_streamed 避免重复正文。
  WebUI 更新当前回答区域，不为候选追加第二个永久气泡。
  验证 nudge 带 internal_scaffold，不会产生普通 user SSE 事件。
  验证工具仍照常产生 tool / tool_complete；验证后的回答继续以 token 流出。

done / 历史：
  Agent durable 保留候选与验证后的最终回答，只过滤 verification nudge。
  WebUI 若发现后续最终回答，则只展示最终回答；若没有后续回答，则展示候选兜底。
```

### 1.2 pre_verify hook：插件要求继续验证

**机制启用状态：⚠️ 条件启用。** 只有本轮有文件变更、已注册 `pre_verify` hook/plugin 且它返回继续验证文案时触发；没有变更、没有 hook 或 hook 返回空结果时不触发。单轮 nudge 数量受 `max_verify_nudges` 限制，默认最多 3 次。

**WebUI 对齐状态：✅ 已对齐。** WebUI 以与 verify-on-stop 相同的规则隐藏插件 nudge，并处理候选替换与兜底。

**上游持久化规则：✅ 一致。** assistant 候选保留；插件注入的 synthetic user nudge 不进入 durable transcript。Fork 仅补充语义标记。

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "把接口改成支持批量上传。", "_turn_key": "turn:2"},
  {"role": "assistant", "content": "批量上传接口已经完成。", "finish_reason": "verification_required"},
  {
    "role": "user",
    "content": "请先运行批量上传兼容性检查，再结束本轮。",
    "_hermes_message_class": "internal_scaffold",
    "_hermes_scaffold_kind": "pre_verify",
    "_pre_verify_synthetic": true
  },
  {"role": "assistant", "content": "批量上传已经完成，兼容性检查也通过了。", "finish_reason": "stop"}
]
```

Agent durable transcript：

```json
[
  {"role": "user", "content": "把接口改成支持批量上传。", "_turn_key": "turn:2"},
  {"role": "assistant", "content": "批量上传接口已经完成。", "finish_reason": "verification_required"},
  {"role": "assistant", "content": "批量上传已经完成，兼容性检查也通过了。", "finish_reason": "stop"}
]
```

WebUI 展示投影：

```json
[
  {"role": "user", "content": "把接口改成支持批量上传。", "_turn_key": "turn:2"},
  {"role": "assistant", "content": "批量上传已经完成，兼容性检查也通过了。", "finish_reason": "stop"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  插件 nudge 没有 user SSE 事件。候选回答通过 token / interim callback 提前展示；
  插件检查和后续最终回答继续以 tool/token 事件呈现。WebUI 复用同一回答区域。

done / 历史：
  Agent durable 保留候选和验证后的最终回答，只过滤插件 nudge。
  WebUI 有最终回答时隐藏候选；没有最终回答时以候选兜底。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

这是 verify-on-stop 的并行机制。区别在于它不是内建验证策略，而是来自 `pre_verify` hook / plugin 的继续要求。

特点：

- 同样把 assistant 候选作为真实内容提前展示并持久化
- 再补带 `internal_scaffold` 的 synthetic `user` nudge
- user nudge 的标记位：`_pre_verify_synthetic`，统一语义为
  `class=internal_scaffold`、`kind=pre_verify`

具体文案形态：

- 没有仓库内置的一条固定默认文案
- 文案来自 `pre_verify` hook / plugin 的 `get_pre_verify_continue_message(...)`
- 因此它通常是 **插件定义的动态 `user` 控制消息**
- 常见形态仍然是 `[System: ...]` 风格的继续验证提示，但不能假定文本固定

### 1.3 这两类消息的持久化策略

**机制启用状态：ℹ️ 非独立开关。** 本节不是新的运行机制；它随 1.1 或 1.2 实际触发后决定候选和 nudge 的持久化边界。

**WebUI 对齐状态：✅ 已对齐。** 本节定义的“nudge 隐藏、真实候选保留”已由 WebUI 的统一投影实现。

**上游持久化规则：✅ 一致。** 两条 continuation 的 durable 边界均保持“真实 assistant 保留、synthetic user nudge 跳过”。

实现位置：`hermes-agent/agent/turn_finalizer.py`

finalizer 只把 verification continuation 的 synthetic user nudge 从 live history /
durable flush 中剔除；真实 assistant 候选必须保留并持久化。

这意味着：

- `user` nudge 是内部控制消息
- assistant 候选答案是真实模型输出；后续验证轮还会产生新的最终答案

因此 verify-on-stop / pre_verify 不是“一对都带 scaffold”：

- `user` 是带 scaffold 标记的 synthetic nudge
- `assistant` 是不带 scaffold 标记的真实待验证候选

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "修复配置解析器读取空值时崩溃的问题。", "_turn_key": "turn:3"},
  {"role": "assistant", "content": "解析器已经修好。", "finish_reason": "verification_required"},
  {"role": "user", "content": "当前没有通过的测试记录，请先验证这次修改。", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "verification_stop", "_verification_stop_synthetic": true},
  {"role": "assistant", "content": "解析器已修复，相关测试全部通过。", "finish_reason": "stop"}
]
```

Agent durable transcript：

```json
[
  {"role": "user", "content": "修复配置解析器读取空值时崩溃的问题。", "_turn_key": "turn:3"},
  {"role": "assistant", "content": "解析器已经修好。", "finish_reason": "verification_required"},
  {"role": "assistant", "content": "解析器已修复，相关测试全部通过。", "finish_reason": "stop"}
]
```

#### 逐行持久化与重载结果

本例在 Agent 内存中实际经过四行：真实 user、待验证 assistant 候选、synthetic user
nudge、验证后的 assistant。写入 durable transcript 时，不是按“前一条 assistant 是否最终
完成”来删除，而是只按 nudge 的 synthetic 语义过滤：

| 运行时行 | 是否写入 SQLite / JSON session log | 重载后含义 |
|---|---|---|
| 真实 user「修复配置解析器…」 | 是 | 本轮唯一真实用户意图和 turn 起点。 |
| assistant 候选「解析器已经修好。」 | 是 | 真实模型输出；保留其原始 content 与已有生成元数据（如 `finish_reason`）。 |
| pre_verify / verify-on-stop user nudge | 否 | 仅驱动下一次验证调用，不能成为历史中的用户请求。 |
| 验证后的 assistant「…测试全部通过。」 | 是 | 本轮后续真实模型输出，作为最终交付候选。 |

因此 Agent 重启或从 `state.db` 重放时，得到的 canonical transcript 是
`user → assistant 候选 → assistant 最终回答`，而不是删除候选后伪造出的
`user → assistant 最终回答`。WebUI 在此基础上做展示投影：有最终回答时显示最终回答；
没有最终回答时才显示最后一条非空候选。该投影不会回写或删除 durable 的候选行。

WebUI 展示投影：

```json
[
  {"role": "user", "content": "修复配置解析器读取空值时崩溃的问题。", "_turn_key": "turn:3"},
  {"role": "assistant", "content": "解析器已修复，相关测试全部通过。", "finish_reason": "stop"}
]
```

**SSE 实时与 `done`**

```text
这不是第三种 SSE 协议：1.1 和 1.2 都遵循同一规则。
真实候选可通过 token / interim callback 提前展示并持久化；内部 user nudge 不会成为
普通 user SSE 消息，也不会进入 durable transcript。WebUI 将候选和最终回答收口到
同一个回答区域，不要求 Agent 删除候选。
```

**实现来源说明**

- 上游原有机制已经会过滤 verification 的 synthetic user nudge，但会保留并
  提前展示待验证的 assistant 候选。
- 本地 Agent fork 的 `93d827f13` 曾进一步把 assistant 候选也标成
  `internal_scaffold`，这偏离了上游语义。当前 `fix-artifact` 本地补丁已撤销
  这两处 assistant 误标，只保留 user nudge 的 `internal_scaffold` 分类。
- WebUI integration 只负责隐藏控制消息和生成可见 transcript，不负责把
  真实候选重新写入 Agent durable store。

**实现状态：✅ 已实现。** Agent 回归已覆盖候选回调、durable 保留、
budget 耗尽复用和 nudge 过滤；WebUI 回归已覆盖最终回答替换候选、
无最终回答时的非空候选兜底。

## 2. 空响应恢复类

这类逻辑用于处理“模型刚执行完工具，却没有给出可见文本”的情况。

### 2.1 工具执行后模型返回空内容：补一对 synthetic `assistant + user`

**机制启用状态：✅ 默认具备（条件触发）。** 工具结果已返回、模型却没有可见内容时进入空响应恢复；正常回答不会追加这对脚手架。

**WebUI 对齐状态：✅ 已对齐。** `internal_scaffold` pair 在实时收口与历史投影中都被过滤。

**上游持久化规则：✅ 一致。** recovery pair 在 SQLite 与 JSON durable 写入出口均跳过；Fork 将 legacy flag 收口为 `internal_scaffold`，不改变结果。

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "查一下今天的错误日志，最常见的问题是什么？", "_turn_key": "turn:42"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_logs", "type": "function", "function": {"name": "search_logs", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_logs", "content": "38 条错误；24 条为连接超时。"},
  {"role": "assistant", "content": "(empty)", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "empty_response_recovery", "_empty_recovery_synthetic": true},
  {"role": "user", "content": "You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task.", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "empty_response_recovery", "_empty_recovery_synthetic": true},
  {"role": "assistant", "content": "今天最常见的是连接超时，共 24 条。", "finish_reason": "stop"}
]
```

Agent durable transcript（恢复成功路径）：

```json
[
  {"role": "user", "content": "查一下今天的错误日志，最常见的问题是什么？", "_turn_key": "turn:42"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_logs", "type": "function", "function": {"name": "search_logs", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_logs", "content": "38 条错误；24 条为连接超时。"},
  {"role": "assistant", "content": "今天最常见的是连接超时，共 24 条。", "finish_reason": "stop"}
]
```

#### 逐行持久化与重载结果

空响应恢复在内存中是 `user → assistant(tool_calls) → tool → (empty) assistant →
synthetic user nudge → assistant 最终回答`。其中前后两段是真实任务轨迹；中间 pair 只为
维持 provider 所要求的角色序列，并驱动下一次模型调用：

| 运行时行 | 是否写入 SQLite / JSON session log | 重载后含义 |
|---|---|---|
| 原始 user 请求 | 是 | 本轮的真实用户意图和 turn 起点。 |
| `assistant(tool_calls)` | 是 | 已发起的真实工具调用，保留调用 ID、名称和参数。 |
| `tool` result | 是 | 对应工具调用的真实执行结果，保留调用链完整性。 |
| `assistant("(empty)")` | 否 | 仅把 `tool → user` 的非法角色跳转补成合法序列。 |
| 英文 synthetic user nudge | 否 | 仅要求模型消费已有工具结果并继续，绝非用户新请求。 |
| 后续 assistant 最终回答 | 是 | 恢复成功后对原始请求的真实交付。 |

所以重启后的 canonical transcript 为 `user → assistant(tool_calls) → tool → assistant
最终回答`。WebUI 只对这份 durable 历史做展示投影，工具显示为工具卡片，最终回答显示为
assistant 气泡；它不会依赖页面刷新时再删除 `(empty)` 或英文 nudge。

#### 持久化规则与边界

这里的“删除”不是等到 WebUI 渲染时才做的文案隐藏，而是 Agent 在写入 durable transcript
时就跳过该 pair。规则按消息的**语义标记**判断，不按正文、角色或它是否仍在列表尾部判断：

1. `assistant("(empty)")` 与 user nudge 都带
   `_empty_recovery_synthetic=True`；本 Fork 当前还会写入
   `_hermes_message_class="internal_scaffold"` 和
   `_hermes_scaffold_kind="empty_response_recovery"`。
2. Agent 的 SQLite 写入（`_flush_messages_to_session_db`）和 JSON session log
   写入（`_save_session_log`）都会跳过 internal scaffold。必须在两处都过滤，因为
   append-only flush 可能发生在恢复尚未结束时；若 nudge 已被模型回答，这对消息会落在
   列表中间，不能只靠“删除尾部”处理。
3. 若这对消息在追加真实最终回答前仍位于内存 `messages` 尾部，循环会将其移除；若恢复后的
   下一次调用又发起工具调用，它们可能已移到列表中间并继续作为本次运行的模型上下文存在。
   两种位置下 durable 写入都会跳过它们；后续持久化的是实际工具轨迹和回答，而不是恢复提示。
4. 恢复最终仍失败时，不存在可保留的“最终回答”。该路径会再使用 terminal `(empty)`
   sentinel 表达失败，并清理内部 scaffold；为避免下一轮从裸 `tool` 结果或缺少结果的
   `assistant(tool_calls)` 开始，尾部清理还可能回退这一段尚未闭合的工具交互。该例外属于
   第 2.2 节“最终空响应 sentinel”，不能用本节的
   “保留工具调用、工具结果和最终回答”描述。

#### 上游与 Fork 的责任划分

- **上游 Hermes Agent 已有的规则**：empty-response recovery pair、legacy
  `_empty_recovery_synthetic` 标记，以及在 SQLite 和 JSON 两个 durable 写入出口跳过该
  pair 的规则，均来自上游 Hermes Agent 的
  `f2a528fb`（`fix(agent): never persist empty-response recovery scaffolding`）。
  因此“不要把这对恢复消息持久化”不是 WebUI Fork 新创造的产品规则。
- **本 Fork 在 Agent 侧的改动**：在保留 legacy flag 的同时，增加 authoritative
  `_hermes_message_class` / `_hermes_scaffold_kind`，并将持久化判定收口为
  `is_internal_scaffold(...)`。这让新旧标记都得到同一结果，也让其它 Agent 控制消息可以
  使用同一套语义，而没有改变 empty-response pair 本来就不持久化的结论。
- **本 Fork 在 WebUI 侧的改动**：对实时收口的 `done.session.messages`、
  `GET /api/session` 等展示投影再次过滤 `internal_scaffold`。这是防御性展示边界：即使收到
  带语义标记的控制行，也不会渲染成用户或助手气泡；它不是 Agent durable 过滤的替代品。

WebUI 展示投影：

```json
[
  {"role": "user", "content": "查一下今天的错误日志，最常见的问题是什么？", "_turn_key": "turn:42"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_logs", "type": "function", "function": {"name": "search_logs", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_logs", "content": "38 条错误；24 条为连接超时。"},
  {"role": "assistant", "content": "今天最常见的是连接超时，共 24 条。", "finish_reason": "stop"}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认具备（条件触发）。** 空响应重试耗尽且没有可用 fallback 时补 terminal sentinel；只要得到有效回答或 fallback 成功，就不会走该出口。

**WebUI 对齐状态：✅ 已对齐。** terminal sentinel 不会被渲染为真实 assistant 气泡。

**上游持久化规则：✅ 一致。** terminal `(empty)` sentinel 不进入 durable transcript；Fork 仅统一其语义分类。

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "运行健康检查脚本并告诉我结果。", "_turn_key": "turn:4"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_health", "function": {"name": "health_check", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_health", "content": "检查完成：数据库连接失败。"},
  {"role": "assistant", "content": "(empty)", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "empty_response_recovery", "_empty_terminal_sentinel": true}
]
```

Agent durable transcript：

```json
[
  {"role": "user", "content": "运行健康检查脚本并告诉我结果。", "_turn_key": "turn:4"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_health", "function": {"name": "health_check", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_health", "content": "检查完成：数据库连接失败。"}
]
```

WebUI 展示投影：

```json
[
  {"role": "user", "content": "运行健康检查脚本并告诉我结果。", "_turn_key": "turn:4"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_health", "function": {"name": "health_check", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_health", "content": "检查完成：数据库连接失败。"}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认具备（条件触发）。** Provider 只返回 reasoning/thinking、没有可见正文时用于下一次重试；普通的可见回答不会触发。

**WebUI 对齐状态：✅ 已对齐。** thinking prefill 不形成 assistant 气泡；后续真实正文正常收口。

**上游持久化规则：✅ 一致。** thinking-only prefill 不进入 durable transcript；Fork 仅统一其语义分类。

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "比较 dev.yaml 和 prod.yaml，告诉我哪个适合生产环境。", "_turn_key": "turn:5"},
  {"role": "assistant", "content": "", "reasoning": "已比较配置差异。", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "thinking_prefill", "_thinking_prefill": true},
  {"role": "assistant", "content": "prod.yaml 更适合生产环境，因为它限制了并发并开启了审计日志。", "finish_reason": "stop"}
]
```

Agent durable transcript：

```json
[
  {"role": "user", "content": "比较 dev.yaml 和 prod.yaml，告诉我哪个适合生产环境。", "_turn_key": "turn:5"},
  {"role": "assistant", "content": "prod.yaml 更适合生产环境，因为它限制了并发并开启了审计日志。", "finish_reason": "stop"}
]
```

WebUI 展示投影：

```json
[
  {"role": "user", "content": "比较 dev.yaml 和 prod.yaml，告诉我哪个适合生产环境。", "_turn_key": "turn:5"},
  {"role": "assistant", "content": "prod.yaml 更适合生产环境，因为它限制了并发并开启了审计日志。", "finish_reason": "stop"}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认执行（分两处收口）。** 主循环在生成真实回答或验证 nudge 前会移除 thinking prefill；持久化出口会跳过所有 `internal_scaffold`，并在空响应 sentinel 被移除后回退悬空的 tool/assistant(tool_calls) 尾部。没有这些消息时为空操作。

**WebUI 对齐状态：✅ 已对齐。** Agent durable 过滤之外，WebUI 仍在展示边界过滤 internal scaffold。

**上游持久化规则：✅ 一致。** 上游尾部清理和 durable 写入跳过规则保留；Fork 仅将分类判定统一为 `internal_scaffold`。

实现位置：`hermes-agent/run_agent.py`

`_drop_trailing_empty_response_scaffolding()` 会在持久化前清理尾部 empty-response 哨兵并回退孤立的 tool / assistant(tool_calls) 结构；更一般的 scaffold 则由 `internal_scaffold` 语义在 flush 时统一跳过。

这类逻辑的关键思想是：

- 运行时可以临时注入内部 scaffold
- durable transcript 不能把这些 scaffold 当成真实对话保留下来

**三段式示例：运行时、Agent durable、WebUI 展示投影**

Agent 运行时 `messages`（清理前）：

```json
[
  {"role": "user", "content": "运行健康检查脚本。", "_turn_key": "turn:6"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_health", "function": {"name": "health_check", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_health", "content": "检查失败：无法连接数据库。"},
  {"role": "assistant", "content": "(empty)", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "empty_response_recovery", "_empty_recovery_synthetic": true},
  {"role": "user", "content": "You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task.", "_hermes_message_class": "internal_scaffold", "_hermes_scaffold_kind": "empty_response_recovery", "_empty_recovery_synthetic": true}
]
```

Agent durable transcript（本例工具尾未闭合）：

```json
[
  {"role": "user", "content": "运行健康检查脚本。", "_turn_key": "turn:6"}
]
```

WebUI 展示投影：

```json
[
  {"role": "user", "content": "运行健康检查脚本。", "_turn_key": "turn:6"}
]
```

**SSE 实时与 `done`**

```text
清理动作本身不产生 SSE。它只改变 Agent 送入持久化和 done 合并前的消息集合；
用户已经收到的 tool / token 事件由终态 session 重新收口，内部 pair 不会补显示。
```

### 2.5 上游 dropped tool-call recovery：补一对不持久化的 assistant + user

**机制启用状态：⛔ 当前 Fork 未接入。** 上游仅在流中断或工具调用内容被丢弃、需要提示模型拆分工具参数时触发；当前 Fork 没有生产、分类或持久化过滤这条路径。

**WebUI 对齐状态：⚠️ 未接入。** 当前 Fork 未生产或分类 `_dropped_toolcall_nudge`，因此不存在可验证的 WebUI 对齐路径。

**上游持久化规则：⚠️ Fork 未接入。** 上游的 assistant + user pair 都不持久化；Fork 当前没有对应生产或 durable 过滤分支，不能宣称一致。

上游当前还有一条本 Fork 尚未实现的恢复路径：模型给出
`finish_reason="tool_calls"`，但解析后的 `tool_calls` 为空。

上游会最多重试三次，并补一对 `_dropped_toolcall_nudge` 消息：叙述式
assistant 与“直接发出实际工具调用”的 user nudge。两条都是 ephemeral scaffold。

它们和 empty-response pair 一样，无论仍在尾部还是已落到列表中间，都不会写入
SQLite 或 JSON durable transcript。恢复成功后，只保留真实工具轨迹和后续回答。

**三段式示例：上游运行时、上游 durable、Fork WebUI 边界**

Agent 运行时 `messages`（上游）：

```json
[
  {"role": "user", "content": "请求执行某项需要工具的任务。"},
  {"role": "assistant", "content": "我将调用工具。", "tool_calls": [], "_dropped_toolcall_nudge": true},
  {"role": "user", "content": "Your previous turn indicated a tool call but none was included. Do not narrate a plan or restate intent — issue the actual tool call now to continue the task.", "_dropped_toolcall_nudge": true},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "target_tool", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_1", "content": "真实执行结果。"},
  {"role": "assistant", "content": "后续真实回答。"}
]
```

Agent durable transcript（上游）：

```json
[
  {"role": "user", "content": "请求执行某项需要工具的任务。"},
  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "target_tool", "arguments": "{}"}}]},
  {"role": "tool", "tool_call_id": "call_1", "content": "真实执行结果。"},
  {"role": "assistant", "content": "后续真实回答。"}
]
```

WebUI 展示投影（当前 Fork 未接入该恢复路径）：

```json
[]
```

**Fork 状态：未实现。** 当前 Fork 的 `conversation_loop` 没有该恢复分支，
`message_semantics` 与持久化过滤表也未识别 `_dropped_toolcall_nudge`。因此它不能被
当作当前 WebUI 已覆盖的消息类型；同步上游时必须同时接入生产、Agent 持久化过滤和
WebUI 投影过滤。

### 2.6 输出截断 continuation：保留 partial assistant，再补内部 user

**机制启用状态：✅ 默认具备（条件触发）。** Provider 返回 `finish_reason="length"` 或 partial-stream stub 且能构造截断 assistant 时触发；最多进行 3 次 continuation，随后进入第 4 次耗尽出口。

**WebUI 对齐状态：✅ 已与当前 Fork 对齐。** WebUI 隐藏 `length_continuation` anchor 并合并 assistant 片段；第 4 次耗尽出口仍保留 Fork 与上游的 durable 形状差异。

**上游持久化规则：⚠️ 部分一致。** 前三次续写均保留 partial、控制 user 与后续回答；第 4 次耗尽时 Fork 保留既有片段和 anchor，上游改写为单条合并后的 length assistant。

实现位置：`hermes-agent/agent/conversation_loop.py`

模型因输出长度上限或流中断而没有完成回答时，Agent 会先保留已有的 partial
assistant，再补一条 user，要求下一次调用从断点继续。在续写最终成功的路径中，上游会
在本轮收尾时持久化已经产生的 partial assistant、continuation user 和最终 assistant；
这些消息不会在每次重试发生时立即单独写入数据库。若连续 4 次仍未完成，上游会先清理
partial/nudge，再只持久化合并后的 `finish_reason="length"` assistant。Fork 则保留
这些中间 durable 行，并只给 continuation user 增加 `context_anchor` 元数据。

#### 触发条件与三种文案

这组消息只在 `finish_reason="length"`、可构造截断 assistant、且该消息没有完整
`tool_calls` 时出现。完整 tool-call 截断走另一条工具重试路径；只有 reasoning、没有
可见正文的 thinking-budget-exhausted 情况也会在此之前直接结束，不会注入 continuation。

| 运行时判定 | 追加的 user 文案意图 | 典型原因 |
|---|---|---|
| 非 `PARTIAL_STREAM_STUB_ID` | “输出长度上限截断，请从断点继续” | Provider 正常返回 `finish_reason="length"`。 |
| `PARTIAL_STREAM_STUB_ID`，无 `dropped_tools` | “网络中断，请从断点继续” | SSE 已开始传输，但没有收到完成事件。 |
| `PARTIAL_STREAM_STUB_ID`，有 `dropped_tools` | “不要用相同大参数重试；拆成小工具调用” | SSE 在工具参数传输中断开，运行时记录了未完整交付的工具名。 |

第三种会列出至多三个工具名，并要求每次工具参数控制在约 8K tokens 内。这里的
`dropped_tools` 是 partial-stream stub 的字段，不是第 2.5 节的
`_dropped_toolcall_nudge`：后者表示 `finish_reason="tool_calls"` 但工具数组为空，
属于另一条上游恢复机制。

#### 重试、持久化与耗尽出口

每次命中本分支都会递增 `length_continue_retries`。第 1、2、3 次截断会追加
`context_anchor/length_continuation` 并重启请求；随后请求的临时输出 token 上限依次
提高到基准的 2、4、8 倍。第 4 次仍截断时不再追加 user prompt，而是返回 partial 结果。

在成功续写时，当前 Fork 保留已输出的 partial assistant、continuation anchor 与最终
assistant；最终回答文本会将 partial 和后续正文拼接。anchor 是 durable model context，
不会发普通 user SSE，也不会由 WebUI 显示为用户气泡。

在第 4 次耗尽时，当前 Fork 仍保留此前的 partial assistant 与 continuation anchors，
并把拼接后的 partial 作为 `final_response` 返回。上游当前则删除本轮的片段/nudge，
只追加一条合并后的 `finish_reason="length"` assistant。因此耗尽出口的 durable 行形状
是 Fork 与上游的已知差异。

#### 第 4 次耗尽时的上游原有行为

上游把每次可见 partial 标记为 `_length_continuation_fragment`，把 continuation user 标记为
`_length_continuation_nudge`。前 3 次仍会把这两类行留在运行时上下文以驱动下一次续写；
第 4 次仍被截断时，会按当前真实 user turn 的边界删除所有这两类临时行，再将
`truncated_response_parts` 拼成一条新的：

```json
{"role":"assistant","content":"<全部已收到的 partial 文本>","finish_reason":"length"}
```

因此上游 durable transcript 在耗尽出口只保留这条合并后的 assistant，不保留中间
partial fragments 或 continuation user。这样下一次恢复不会再次向模型发送尚未得到回答的
continuation nudge；代价是它不保留每一次截断和续写的逐行轨迹。Fork 选择保留这些
durable 行并以 WebUI 投影收口，故两侧的持久化行数和重放形状不同。

上游 Agent 运行时 `messages`（第 4 次仍截断、清理后的返回态）：

```json
[
  {"role":"user","content":"给我完整列出迁移步骤和回滚方案。","_turn_key":"turn:7"},
  {"role":"assistant","content":"第一步创建新表并启用双写；第二步……第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。","finish_reason":"length"}
]
```

上游 Agent durable transcript：

```json
[
  {"role":"user","content":"给我完整列出迁移步骤和回滚方案。","_turn_key":"turn:7"},
  {"role":"assistant","content":"第一步创建新表并启用双写；第二步……第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。","finish_reason":"length"}
]
```

上游 WebUI 展示投影：

```json
[
  {"role":"user","content":"给我完整列出迁移步骤和回滚方案。","_turn_key":"turn:7"},
  {"role":"assistant","content":"第一步创建新表并启用双写；第二步……第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。","finish_reason":"length"}
]
```

#### 当前 Fork 行为（第 1 次截断后续写成功）

Agent 运行时 `messages`：

```json
[
  {"role": "user", "content": "给我完整列出迁移步骤和回滚方案。", "_turn_key": "turn:7"},
  {"role": "assistant", "content": "第一步创建新表并启用双写；第二步……", "finish_reason": "length"},
  {"role": "user", "content": "[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]", "_hermes_message_class": "context_anchor", "_hermes_scaffold_kind": "length_continuation"},
  {"role": "assistant", "content": "第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。", "finish_reason": "stop"}
]
```

Agent durable transcript：

```json
[
  {"role": "user", "content": "给我完整列出迁移步骤和回滚方案。", "_turn_key": "turn:7"},
  {"role": "assistant", "content": "第一步创建新表并启用双写；第二步……", "finish_reason": "length"},
  {"role": "user", "content": "[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]", "_hermes_message_class": "context_anchor", "_hermes_scaffold_kind": "length_continuation"},
  {"role": "assistant", "content": "第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。", "finish_reason": "stop"}
]
```

WebUI 展示投影：

```json
[
  {"role": "user", "content": "给我完整列出迁移步骤和回滚方案。", "_turn_key": "turn:7"},
  {"role": "assistant", "content": "第一步创建新表并启用双写；第二步……第三步回填历史数据；第四步灰度切流；最后保留一周回滚窗口。", "finish_reason": "stop"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  partial 已经以 token 发出；context anchor 不产生 user SSE。
  后续文本继续以 token 到达，实时 DOM 可能表现为同一段回答的续写。

done / 历史：
  Agent durable 保留 partial、控制 user 和续写回答。WebUI 隐藏 anchor，并按原顺序
  合并 assistant 片段，只生成一个最终回答气泡。
```

**实现状态：✅ 已实现。** continuation user 仅增加
`context_anchor/length_continuation` 元数据，仍按上游 durable 语义保存；
WebUI 隐藏 anchor，并按原顺序合并 partial 与续写。

## 3. “继续别停”类

这类逻辑用于处理模型“看起来像要结束，但实际上还没完成任务”的场景。

### 3.1 Codex intermediate ack continuation

**机制启用状态：⚠️ 条件启用。** 默认 `intent_ack_continuation=auto` 时仅对 `codex_responses` 生效；还需有可用工具、尚未执行工具且候选命中 intermediate-ack 检测，每轮最多 2 次。显式 `true` 可扩展到所有 API 模式，`false` 则关闭。

**WebUI 对齐状态：✅ 已对齐。** ack 留作候选，continue anchor 隐藏；最终回答优先，缺失时使用非空 ack 兜底。

**上游持久化规则：✅ 一致。** 中间 ack、工具轨迹和最终回答均保留；continue user 仍为 durable 行，Fork 只新增 `context_anchor` 元数据。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查项目里所有没有处理的 TODO。","_turn_key":"turn:8"},
  {"role":"assistant","content":"我接下来会搜索整个项目。","finish_reason":"incomplete"},
  {"role":"user","content":"[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_todo","function":{"name":"search_todos","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_todo","content":"找到 5 个 TODO，其中 2 个位于发布流程。"},
  {"role":"assistant","content":"共找到 5 个 TODO，其中 2 个会影响发布。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查项目里所有没有处理的 TODO。","_turn_key":"turn:8"},
  {"role":"assistant","content":"我接下来会搜索整个项目。","finish_reason":"incomplete"},
  {"role":"user","content":"[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"intent_ack_continuation"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_todo","function":{"name":"search_todos","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_todo","content":"找到 5 个 TODO，其中 2 个位于发布流程。"},
  {"role":"assistant","content":"共找到 5 个 TODO，其中 2 个会影响发布。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查项目里所有没有处理的 TODO。","_turn_key":"turn:8"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_todo","function":{"name":"search_todos","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_todo","content":"找到 5 个 TODO，其中 2 个位于发布流程。"},
  {"role":"assistant","content":"共找到 5 个 TODO，其中 2 个会影响发布。"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  「我接下来会搜索整个项目」可能先作为 token / interim 内容出现。WebUI 将它放在
  当前回答区域，不创建永久气泡。context anchor 不产生 user SSE；工具和最终结论
  继续走 tool / token 事件。

done / 历史：
  Agent durable 保留中间 ack、控制 anchor、工具记录和最终结论。WebUI 隐藏 anchor；
  有最终回答时只展示最终回答，没有最终回答时才回退显示最后一条可用 ack。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

某些 Codex 风格模型会先给一句“我接下来会去做 X”，但还没真的调用工具。Agent 检测到这种中间 ack 后，会：

1. 保留一条 assistant incomplete message
2. 再补一条 `user`：

```text
[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]
```

特点：

- assistant ack 沿用上游普通 durable assistant，不标成 `internal_scaffold`
- continue user 目标语义为持久化的 `context_anchor`
- WebUI 负责将过程 ack 与最终回答收口为一个回答区域

因此它不是“一对都从 durable 删除”的 scaffold；隐藏属于 WebUI 投影语义。

文案信息：

user：

```text
[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]
```

assistant：

- 没有固定字符串
- 是模型刚刚输出的那句“中间 ack / 承诺式回复”，例如“我现在去检查目录并总结 3 个重点”
- 这条 assistant 一般会带 `finish_reason="incomplete"`

**实现状态：✅ 已实现。** assistant ack 已恢复为上游普通 durable
assistant；continue user 仅增加 `context_anchor/intent_ack_continuation` 元数据。
WebUI 有最终回答时以最终回答替换 ack，否则保留最后一条非空 ack 兜底。

### 3.2 kanban worker stop guard

**机制启用状态：⚠️ 条件启用。** 仅 Kanban worker 到达停止/交付边界时触发；普通 WebUI 对话或非 Kanban worker 不会追加这对 guard 消息。

**WebUI 对齐状态：⚠️ 部分对齐。** pair 的终态/历史过滤已完成；已在流中送达的候选文本仍未完成专门验证。

**上游持久化规则：✅ 一致。** assistant + user guard pair 都是 ephemeral scaffold，不进入 durable transcript；Fork 仅统一其语义分类。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"完成看板里的登录迁移任务，并提交结果。","_turn_key":"turn:9"},
  {"role":"assistant","content":"报告已经写好，我马上结束任务。","_hermes_message_class":"internal_scaffold","_hermes_scaffold_kind":"kanban_stop","_kanban_stop_synthetic":true},
  {"role":"user","content":"[System: You are a Hermes kanban worker. A plain-text reply is NOT a terminal state for the board.\n\nTask `<task_id>` is still `running`. Ending now without a board tool causes a protocol violation (clean exit with no `kanban_complete` / `kanban_block`).\n\nDo this immediately in your next response — do not narrate intent:\n1. Finish any remaining deliverable (write the required file(s) now).\n2. Call `kanban_complete(summary=..., artifacts=[...])` if the work is done, OR `kanban_block(reason=...)` if you are blocked.\n\nNever end a turn with only a promise of future action. Repeated protocol violations will block this task and require manual intervention.]","_hermes_message_class":"internal_scaffold","_hermes_scaffold_kind":"kanban_stop","_kanban_stop_synthetic":true},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_kanban","function":{"name":"kanban_complete","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_kanban","content":"任务已标记为完成。"},
  {"role":"assistant","content":"登录迁移任务已完成，结果写在 report.md。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"完成看板里的登录迁移任务，并提交结果。","_turn_key":"turn:9"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_kanban","function":{"name":"kanban_complete","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_kanban","content":"任务已标记为完成。"},
  {"role":"assistant","content":"登录迁移任务已完成，结果写在 report.md。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"完成看板里的登录迁移任务，并提交结果。","_turn_key":"turn:9"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_kanban","function":{"name":"kanban_complete","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_kanban","content":"任务已标记为完成。"},
  {"role":"assistant","content":"登录迁移任务已完成，结果写在 report.md。"}
]
```

**SSE 实时与 `done`**

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

Task `<task_id>` is still `running`. Ending now without a board tool causes a protocol violation (clean exit with no `kanban_complete` / `kanban_block`).

Do this immediately in your next response — do not narrate intent:
1. Finish any remaining deliverable (write the required file(s) now).
2. Call `kanban_complete(summary=..., artifacts=[...])` if the work is done, OR `kanban_block(reason=...)` if you are blocked.

Never end a turn with only a promise of future action. Repeated protocol violations will block this task and require manual intervention.]
```

assistant：

- 没有固定字符串
- 它是模型原本那条“准备结束但还没调用 kanban terminal tool”的回答
- 但在这个场景里，这条 assistant 也会被标成 `_kanban_stop_synthetic`

### 3.3 Codex Responses incomplete continuation：必要时补 assistant 和 user nudge

**机制启用状态：⚠️ 条件启用。** 仅 `api_mode="codex_responses"` 且 Provider 返回未完成状态、未完成 item 或仅 reasoning/commentary 而无最终回答时触发；单轮最多 3 次。只有 interim 没有可供 Responses API 重放的正文、reasoning item 或 message item，且当前尾部是 assistant 时才追加 user nudge；可重放时直接重试，不追加 nudge。

**WebUI 对齐状态：✅ 已对齐。** incomplete assistant 按候选收口，nudge anchor 隐藏，且有无最终回答均有确定投影。

**上游持久化规则：✅ 一致。** incomplete assistant、控制 user 与最终回答均保留；Fork 只给控制 user 增加 `context_anchor` 元数据。

实现位置：`hermes-agent/agent/conversation_loop.py`

Codex Responses 返回 `finish_reason="incomplete"` 时，Agent 会先保留可重放的
incomplete assistant。若这条 assistant 没有可供 Responses API 重放的内容，且尾部仍是
assistant，才补一条 user nudge，请模型直接给出最终答案；若尾部不是 assistant，则为了
避免 user→user 或 tool→user 非法序列而不追加。目标接缝将 nudge 标成持久化的
`context_anchor`，不删除上游 durable 内容。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"比较两套缓存方案，给出选择建议。","_turn_key":"turn:10"},
  {"role":"assistant","content":"我已比较命中率和失效策略，但还没有写出结论。","finish_reason":"incomplete"},
  {"role":"user","content":"[System: Your previous response contained only internal reasoning and never produced a visible answer or tool call. Do not keep thinking. Produce your final answer as plain text now (or make the tool call you were planning).]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"codex_incomplete_nudge"},
  {"role":"assistant","content":"建议选方案 B：命中率略低，但失效和回滚更可控。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"比较两套缓存方案，给出选择建议。","_turn_key":"turn:10"},
  {"role":"assistant","content":"我已比较命中率和失效策略，但还没有写出结论。","finish_reason":"incomplete"},
  {"role":"user","content":"[System: Your previous response contained only internal reasoning and never produced a visible answer or tool call. Do not keep thinking. Produce your final answer as plain text now (or make the tool call you were planning).]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"codex_incomplete_nudge"},
  {"role":"assistant","content":"建议选方案 B：命中率略低，但失效和回滚更可控。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"比较两套缓存方案，给出选择建议。","_turn_key":"turn:10"},
  {"role":"assistant","content":"建议选方案 B：命中率略低，但失效和回滚更可控。"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  incomplete assistant 如果含可见 commentary，会经 interim_assistant 或已发 token 显示。
  context anchor 没有 user SSE；后续完整答案继续以 token / done 呈现。

done / 历史：
  Agent durable 保留可重放 incomplete assistant、控制 anchor 和最终回答。WebUI 有最终
  回答时隐藏 incomplete；没有最终回答时回退显示最后一条可用 incomplete 内容。
```

**实现状态：✅ 已实现。** nudge 仅增加
`context_anchor/codex_incomplete_nudge` 元数据，按上游 durable 语义保存；
WebUI 隐藏 nudge，并在有最终回答时替换 incomplete、无最终回答时使用非空
incomplete 兜底。

### 3.4 异步后台子任务：`delegate_task` 派发与完成唤醒

**机制启用状态：⚠️ 条件启用。** 交互式 CLI / Gateway 中，顶层 `delegate_task` 默认自动
后台派发，工具会立即返回 delegation handle；不需要也不能通过 `background=false` 改回顶层
同步模式。默认最多同时运行 3 个子任务，容量满时回退为同步执行。one-shot / stateless
通道没有后续队列消费能力，会显式走同步路径，避免后台结果无人接收；嵌套 orchestrator
子任务仍在当前调用内等待结果。

**WebUI 对齐状态：✅ 已对齐（Fork 专用唤醒接缝）。** WebUI 为每个 delegation 保存
`delegation_id → origin_turn_key` 侧车映射。完成事件只在会话空闲时启动专用唤醒轮次；前台
轮次忙时先排队，不把结果插入正在进行的 tool → assistant 序列。唤醒用隐藏的
`context_anchor/async_delegation_completion` user 承载完成通知，结果 assistant/tool 在
展示副本中回挂原始 `turn_key`，因此不会出现第二个可见 user 气泡。

**上游持久化规则：⚠️ 基线一致，Fork 扩展归属元数据。** 上游会持久化真实的
`delegate_task` assistant/tool 派发轨迹，以及完成唤醒后模型生成的真实 assistant 结果。
完成通知本身是控制上下文，不是新的人工请求；当前 Fork 将它作为结构化
`context_anchor` 保存（持久化路径开启时），并把 origin turn、wakeup 状态和重试信息放在
WebUI 侧车，而不是污染 Agent transcript。上游基础实现没有 Fork 的
`async_delegation_origins`、显式 `turn_key_override` 和 `_background_task_ids` 展示字段。

实现位置：
`hermes-agent/tools/async_delegation.py`、`hermes-agent/tools/delegate_tool.py`、
`hermes-agent/tools/process_registry.py`、`api/background_process.py`、
`integration/async_delegation_turns/state.py`、`api/streaming.py`、
`integration/agent_message_semantics/projection.py`。

用户请求“请并行检查登录接口和缓存配置，完成后汇总”。父 Agent 先派发两个后台子任务，
随后可以结束当前轮；子任务完成后，空闲会话再被唤醒。下面三个数组省略了两次调用之间
与本机制无关的历史消息，但保留了消息顺序和控制字段。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"请并行检查登录接口和缓存配置，完成后汇总。","_turn_key":"turn:31"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delegate","function":{"name":"delegate_task","arguments":"{\"tasks\":[{\"goal\":\"检查登录接口\"},{\"goal\":\"检查缓存配置\"}]}"}}]},
  {"role":"tool","tool_call_id":"call_delegate","content":"{\"status\":\"dispatched\",\"mode\":\"background\",\"count\":2,\"delegation_id\":\"deleg_7f3a1c2b\",\"goals\":[\"检查登录接口\",\"检查缓存配置\"]}"},
  {"role":"assistant","content":"我已派发两个后台子任务，完成后会继续汇总结果。"},
  {"role":"user","content":"[ASYNC DELEGATION BATCH COMPLETE — deleg_7f3a1c2b]\n\n2 个后台子任务已完成。\n\n--- TASK 1: 检查登录接口 ---\n状态：completed\n摘要：未发现新的超时回归。\n\n--- TASK 2: 检查缓存配置 ---\n状态：completed\n摘要：已启用失效保护。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"async_delegation_completion","_source":"async_delegation_wakeup","delegation_id":"deleg_7f3a1c2b"},
  {"role":"assistant","content":"后台检查完成：登录接口未发现新的超时回归，缓存配置已启用失效保护。"}
]
```

这不是同一时刻连续追加的普通 user→assistant 对话：前四行属于首次派发轮次，完成通知
是在会话空闲后由 WebUI 专用 scheduler 发起的新调用。唤醒 user 行的作用是把完整、可重试
的子任务结果交给模型；它不代表用户又输入了一句话。

Agent durable transcript：

```json
[
  {"role":"user","content":"请并行检查登录接口和缓存配置，完成后汇总。","_turn_key":"turn:31"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delegate","function":{"name":"delegate_task","arguments":"{\"tasks\":[{\"goal\":\"检查登录接口\"},{\"goal\":\"检查缓存配置\"}]}"}}]},
  {"role":"tool","tool_call_id":"call_delegate","content":"{\"status\":\"dispatched\",\"mode\":\"background\",\"count\":2,\"delegation_id\":\"deleg_7f3a1c2b\",\"goals\":[\"检查登录接口\",\"检查缓存配置\"]}"},
  {"role":"assistant","content":"我已派发两个后台子任务，完成后会继续汇总结果。"},
  {"role":"user","content":"[ASYNC DELEGATION BATCH COMPLETE — deleg_7f3a1c2b]\n\n2 个后台子任务已完成。\n\n--- TASK 1: 检查登录接口 ---\n状态：completed\n摘要：未发现新的超时回归。\n\n--- TASK 2: 检查缓存配置 ---\n状态：completed\n摘要：已启用失效保护。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"async_delegation_completion","_source":"async_delegation_wakeup","delegation_id":"deleg_7f3a1c2b"},
  {"role":"assistant","content":"后台检查完成：登录接口未发现新的超时回归，缓存配置已启用失效保护。"}
]
```

持久化的是首次派发的真实 assistant/tool 轨迹、父 Agent 已输出的真实回答，以及唤醒后
生成的真实 assistant 结果。完成通知 anchor 即使被保存，也只是模型上下文控制行；它不应
被当成新的人工 turn。`async_delegation_origins`、claim / retry 状态和完成时间属于 WebUI
侧车状态，不是 durable transcript 的普通消息。

WebUI 展示投影：

```json
[
  {"role":"user","content":"请并行检查登录接口和缓存配置，完成后汇总。","_turn_key":"turn:31","_background_task_ids":["deleg_7f3a1c2b"]},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delegate","function":{"name":"delegate_task","arguments":"{\"tasks\":[{\"goal\":\"检查登录接口\"},{\"goal\":\"检查缓存配置\"}]}"}}]},
  {"role":"tool","tool_call_id":"call_delegate","content":"{\"status\":\"dispatched\",\"mode\":\"background\",\"count\":2,\"delegation_id\":\"deleg_7f3a1c2b\",\"goals\":[\"检查登录接口\",\"检查缓存配置\"]}"},
  {"role":"assistant","content":"我已派发两个后台子任务，完成后会继续汇总结果。"},
  {"role":"assistant","content":"后台检查完成：登录接口未发现新的超时回归，缓存配置已启用失效保护。","_turn_key":"turn:31","_source":"async_delegation_wakeup","delegation_id":"deleg_7f3a1c2b"}
]
```

展示层隐藏 `async_delegation_completion` anchor，但保留派发工具卡片、父 Agent 的真实
回答和完成后的真实 assistant 结果。完成结果在展示副本中带原始 `turn:31`，并通过
`_background_task_ids` 给原用户行关联后台任务；这些字段不回写 Agent canonical message。

**SSE 实时与 `done`**

```text
派发阶段：tool / tool_complete 正常产生；background_task_status=running 只更新后台任务状态，
不会生成普通 user 气泡。子任务完成后，若会话空闲，专用 scheduler 领取 completion event，
启动 source=async_delegation_wakeup 的新 stream；anchor 不推送为 user SSE，最终 assistant
继续以 token 流出并以 done 收口。

若前台轮次仍在运行：completion 保持 queued，事件释放 claim 后重新入队；不会把完成文本
插入正在执行的 tool→assistant 中间。若进程重启：SQLite async_delegations 恢复 pending
记录，再按 claim / ack 机制重试；origin 映射缺失时 fail closed，不猜测 transcript 顺序。
```

**可靠性边界：**

- `delegate_task` 返回 `rejected` 或容量达到上限时，不能把“已派发”当成成功；Agent 会回退
  同步执行，或将失败状态交给模型说明。
- 完成通知依赖 `delegation_id → origin_turn_key` 侧车映射。映射缺失、重复 claim 或会话
  被删除时，优先重试 / 标记失败，不能把结果挂到最近一条 user。
- 完成摘要可能很长，会占用下一次模型上下文；它虽然不展示为 user，但仍需按普通输入做
  长度和敏感信息控制。
- `_background_task_ids` 是展示元数据，不是模型可依赖的事实；真正的完成状态以 durable
  delegation 记录和 completion event 为准。

**实现状态：✅ 已实现。** 当前 Fork 已接入专用后台唤醒、显式 origin turn 绑定、隐藏
`context_anchor` 和恢复/重试状态管理；`one-shot / stateless` 是有意关闭异步回灌、改走
同步执行的例外路径。

### 3.5 `terminal(background=true)`：后台进程完成与 watch-pattern 唤醒（未实现）

**机制启用状态：⚠️ 条件启用。** 只有 `terminal(background=true)` 同时配置
`notify_on_complete=true`，或配置了 `watch_patterns` 时才会向 completion queue 投递事件。
`watch_patterns` 每个进程最多约 15 秒通知一次，连续触发抑制后会自动关闭 watch 并提升为
完成通知；普通前台 terminal 不触发这条路径。

**WebUI 对齐状态：⚠️ 部分对齐。** Agent / CLI 会把完成事件格式化成
`[IMPORTANT: Background process ...]`，WebUI 也会在空闲时启动 `source=process_wakeup`
轮次。但当前 Fork 只对 `async_delegation_wakeup` 加 `context_anchor`；普通
`process_wakeup` 没有结构化隐藏标记，也没有原始 turn 的显式绑定，可能展示为新的 user 气泡。

**上游持久化规则：✅ 基线一致，Fork 展示语义未完全对齐。** 后台进程的真实 tool 派发
和后续 assistant 结果属于可恢复轨迹。completion / watch 文案作为 server-side wakeup 的
user 输入时会形成普通 durable user；如果事件恰好在下一次真实用户请求期间被消费，则只
拼入该次 provider 请求副本，不单独持久化一条通知 user。Fork 增加 `_source=process_wakeup`
和 `_wakeup_meta`，但尚未把它们提升为 `context_anchor`。

实现位置：
`hermes-agent/tools/process_registry.py`、`hermes-webui/api/background_process.py`、
`hermes-webui/api/streaming.py`、`hermes-webui/api/routes.py`。

接缝改造的实施顺序、跨仓库状态归属、风险与验收矩阵见
[`background-process-notification-seam-plan.md`](background-process-notification-seam-plan.md)。

用户先要求“运行登录回归测试”，Agent 通过后台 terminal 派发测试；进程结束后，WebUI
空闲唤醒 Agent。下面的六行跨越了首次派发和后续唤醒两个时间点。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"status\":\"running\",\"session_id\":\"proc_4a7c\",\"background\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=0).\nCommand: pytest tests/test_login.py\nOutput:\n42 passed, 1 failed: test_refresh_token_timeout]"},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

这条唤醒 user 不是人类新输入，但当前普通 `process_wakeup` 路径没有
`_hermes_message_class=context_anchor`。它会获得一个新的 `_turn_key`，与
`async_delegation_wakeup` 的“回挂原 turn”行为不同。

Agent durable transcript：

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_terminal","function":{"name":"terminal","arguments":"{\"command\":\"pytest tests/test_login.py\",\"background\":true,\"notify_on_complete\":true}"}}]},
  {"role":"tool","tool_call_id":"call_terminal","content":"{\"status\":\"running\",\"session_id\":\"proc_4a7c\",\"background\":true}"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=0).\nCommand: pytest tests/test_login.py\nOutput:\n42 passed, 1 failed: test_refresh_token_timeout]","_source":"process_wakeup","_wakeup_meta":{"type":"completion","task_id":"proc_4a7c","exit_code":0}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"运行登录回归测试，完成后告诉我失败用例。","_turn_key":"turn:32"},
  {"role":"assistant","content":"测试已在后台运行，完成后我会汇总失败用例。"},
  {"role":"user","content":"[IMPORTANT: Background process proc_4a7c completed (exit_code=0).\nCommand: pytest tests/test_login.py\nOutput:\n42 passed, 1 failed: test_refresh_token_timeout]","_source":"process_wakeup","_turn_key":"turn:33","_wakeup_meta":{"type":"completion","task_id":"proc_4a7c","exit_code":0}},
  {"role":"assistant","content":"后台测试完成：42 项通过，1 项失败，失败用例是 test_refresh_token_timeout。"}
]
```

上面的 WebUI 投影明确展示了当前风险：`process_wakeup` 会成为新的普通 user turn。
如果通知是在真实用户下一次发送时被消费，通知只会拼进本次模型请求，持久化仍只保存真实
用户文本；如果会话空闲触发 server-side wakeup，则会保存并展示这条内部 user。

**SSE 实时与 `done`**

```text
terminal 派发阶段产生 tool / tool_complete；后台退出或 watch 命中由 completion_queue
承载，不直接产生普通 user SSE。空闲时 server-side process_wakeup 启动新 stream，当前
Fork 的 user anchor 未被统一隐藏，因此可能出现第二个 user 气泡；完成后的 assistant
仍以 token 和 done 收口。
```

**实现状态：⚠️ 机制已实现，WebUI 语义待补齐。** 需要把普通 process wakeup 与
async delegation 一样标成可隐藏的 `context_anchor`，并决定是沿用新 turn，还是建立
`process_id → origin_turn_key` 的显式归属；不能继续仅依赖 `_source` 或通知正文识别。

## 4. 预算耗尽总结类

### 4.1 max iterations：补一条 `user` 总结请求

**机制启用状态：✅ 默认具备（条件触发）。** API 调用次数达到 `max_iterations`，或 `iteration_budget.remaining` 耗尽且 finalizer 判定本轮可走预算兜底时触发；中断、失败或已有正常最终回答时不会追加总结请求。

**WebUI 对齐状态：✅ 已对齐。** summary request anchor 被隐藏，真实 summary assistant 保留为本轮结果。

**上游持久化规则：✅ 一致。** summary request 与 summary assistant 均为 durable 行；Fork 只给 request 增加 `context_anchor` 元数据。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"把这个仓库的依赖和测试都检查一遍。","_turn_key":"turn:11"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_tests","function":{"name":"run_tests","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_tests","content":"测试 126 项，通过 124 项，失败 2 项。"},
  {"role":"user","content":"You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"max_iteration_summary_request"},
  {"role":"assistant","content":"目前有 2 个失败项，分别是登录超时和缓存清理。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"把这个仓库的依赖和测试都检查一遍。","_turn_key":"turn:11"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_tests","function":{"name":"run_tests","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_tests","content":"测试 126 项，通过 124 项，失败 2 项。"},
  {"role":"user","content":"You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"max_iteration_summary_request"},
  {"role":"assistant","content":"目前有 2 个失败项，分别是登录超时和缓存清理。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"把这个仓库的依赖和测试都检查一遍。","_turn_key":"turn:11"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_tests","function":{"name":"run_tests","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_tests","content":"测试 126 项，通过 124 项，失败 2 项。"},
  {"role":"assistant","content":"目前有 2 个失败项，分别是登录超时和缓存清理。"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  「已达到工具调用上限，请总结」是 context anchor，没有 user SSE 事件。
  对普通 Chat Completions / Anthropic summary 路径，汇总调用是非流式的，通常直到 done
  才出现总结；Codex Responses summary 路径可继续发 token。

done / 历史：
  Agent durable 保留 summary request anchor 和真实 summary assistant；WebUI 只隐藏 anchor。
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

**机制启用状态：⚠️ 条件启用。** 只有预算/迭代耗尽且 finalizer 判定仍有可总结上下文、允许执行 fallback summary 时才调用模型生成；若 summary fallback 不可用，则返回本地失败说明。

**WebUI 对齐状态：✅ 已对齐。** 只显示 summary assistant 或明确失败结果，不显示内部总结请求。

**上游持久化规则：✅ 一致。** 预算总结 assistant 作为真实终态回答保留；Fork 未改变其 durable 规则。

Agent 运行时 `messages`（成功）：

```json
[
  {"role":"user","content":"检查依赖和测试，最后给我一份结论。","_turn_key":"turn:12"},
  {"role":"user","content":"You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"max_iteration_summary_request"},
  {"role":"assistant","content":"依赖没有安全漏洞；测试有 2 项失败，需要修复登录超时和缓存清理。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查依赖和测试，最后给我一份结论。","_turn_key":"turn:12"},
  {"role":"user","content":"You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"max_iteration_summary_request"},
  {"role":"assistant","content":"依赖没有安全漏洞；测试有 2 项失败，需要修复登录超时和缓存清理。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查依赖和测试，最后给我一份结论。","_turn_key":"turn:12"},
  {"role":"assistant","content":"依赖没有安全漏洞；测试有 2 项失败，需要修复登录超时和缓存清理。"}
]
```

**SSE 实时与 `done`**

```text
这一小类的 assistant summary 不是 scaffold，而是最终交付。它是否逐字显示取决于
provider：Codex Responses 可流式；其它 summary 分支可能只在 done 一次性出现。
无论哪种，WebUI 都隐藏总结请求，只显示一个最终 assistant 回答。
```

如果模型成功给出总结，Agent 还会再补一条 assistant 总结消息。

特点：

- assistant 总结是要保留的真实交付
- summary request 目标语义是持久化的 `context_anchor`，不应成为 user 气泡

文案信息：

- `user` 侧是固定总结请求
- `assistant` 侧没有固定模板，它是模型基于当前上下文生成的自然语言总结

**实现状态：✅ 已实现。** summary request 仅增加
`context_anchor/max_iteration_summary_request` 元数据，按上游 durable 语义保存；
WebUI 隐藏该 anchor，保留真实 summary assistant 作为本轮回答。
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

**机制启用状态：⚠️ 条件触发。** 流被取消、中断或连接异常且已经收到可见 assistant 内容时触发；没有已收到正文时不会生成 partial assistant 行。

**WebUI 对齐状态：✅ 已对齐。** 已流出的 partial 会作为真实 transcript 内容保留，而不会被误投影为新 user 输入。

**上游持久化规则：✅ 一致。** 已输出的 partial assistant 属于真实 durable 轨迹，Fork 未将其改为 scaffold。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"生成一份完整的数据库迁移方案。","_turn_key":"turn:13"},
  {"role":"assistant","content":"迁移分为准备、双写、切流和回滚四个阶段。准备阶段需要……"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"生成一份完整的数据库迁移方案。","_turn_key":"turn:13"},
  {"role":"assistant","content":"迁移分为准备、双写、切流和回滚四个阶段。准备阶段需要……"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"生成一份完整的数据库迁移方案。","_turn_key":"turn:13"},
  {"role":"assistant","content":"迁移分为准备、双写、切流和回滚四个阶段。准备阶段需要……"}
]
```

**SSE 实时与 `done`**

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

### 5.1.1 中断时跳过尚未执行的工具：补取消的 tool result

**机制启用状态：⚠️ 条件触发。** 中断发生在 assistant 已声明多个工具、但部分工具尚未执行时，为每个未执行调用补取消结果；没有待执行工具时不触发。

**WebUI 对齐状态：⚠️ 部分对齐。** 取消 tool result 不会被 scaffold 过滤；“已取消”专用工具卡片仍未完成验证。

**上游持久化规则：✅ 一致。** 为未执行工具补写的取消 `tool` result 是可恢复的真实轨迹，会进入 durable transcript。

实现位置：`hermes-agent/agent/tool_executor.py`

若用户在工具批次开始前停止，或在顺序执行中途停止，Agent 不会启动剩余工具。
它会为每个未执行的 tool call 补一条 `tool` result，并标明该调用没有产生外部效果。

```text
[Tool execution cancelled — <tool_name> was skipped due to user interrupt]
```

这些 `tool` result 没有 `internal_scaffold` 标记，且会随工具进度写入 session。
它们不是新的用户输入；展示层应将其作为已取消的工具卡片，而不能误报为工具执行成功。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查 staging 环境并部署。","_turn_key":"turn:14"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_check","function":{"name":"deployment_check","arguments":"{}"}},{"id":"call_deploy","function":{"name":"deploy","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_check","content":"检查通过。"},
  {"role":"tool","tool_call_id":"call_deploy","content":"[Tool execution cancelled — deploy was skipped due to user interrupt]"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查 staging 环境并部署。","_turn_key":"turn:14"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_check","function":{"name":"deployment_check","arguments":"{}"}},{"id":"call_deploy","function":{"name":"deploy","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_check","content":"检查通过。"},
  {"role":"tool","tool_call_id":"call_deploy","content":"[Tool execution cancelled — deploy was skipped due to user interrupt]"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查 staging 环境并部署。","_turn_key":"turn:14"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_check","function":{"name":"deployment_check","arguments":"{}"}},{"id":"call_deploy","function":{"name":"deploy","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_check","content":"检查通过。"},
  {"role":"tool","tool_call_id":"call_deploy","content":"[Tool execution cancelled — deploy was skipped due to user interrupt]"}
]
```

**SSE 实时与 `done`**

```text
补写本身不产生 user SSE。工具是否已显示为卡片由具体 tool / stream 事件决定；
done 与历史中的 tool result 则必须保留“已取消、未执行”的事实。
```

### 5.1.2 中断后 transcript 以 tool 结尾：补 assistant 闭合行

**机制启用状态：✅ 默认收尾修复开启（条件触发）。** finalizer 发现 durable transcript 以 tool result 结尾、缺少合法 assistant 闭合行时触发；正常以 assistant 结束的回合不触发。

**WebUI 对齐状态：✅ 已对齐。** 闭合 assistant 按真实终态回答投影，不会被隐藏。

**上游持久化规则：✅ 一致。** 为合法收尾补写的 assistant 是真实 durable 行，Fork 未改变该规则。

实现位置：`hermes-agent/agent/message_sanitization.py`、`hermes-agent/agent/turn_finalizer.py`

中断可能发生在工具完成后、模型生成收尾回答前。若 `messages` 尾部是 `tool`，
Agent 会补一条 assistant，避免下次真实 user 输入形成 `tool → user` 的非法序列。

```text
Operation interrupted.
```

重试等待或错误处理路径会传入更具体的中断原因，并优先使用该文本。
这条 assistant 不是 internal scaffold；它是可持久化的收尾状态，不能被 WebUI 过滤掉。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"读取最新的构建日志并总结失败原因。","_turn_key":"turn:15"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"读取最新的构建日志并总结失败原因。","_turn_key":"turn:15"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"读取最新的构建日志并总结失败原因。","_turn_key":"turn:15"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

**SSE 实时与 `done`**

```text
工具的执行结果按 tool / tool_complete 事件到达。中断收尾 assistant 通常由 Agent 在
终态补写，不会重新发送已不存在的模型 token；done 携带 durable transcript 后，WebUI
把该真实 assistant 显示在原 user turn 的回答区域。
```

### 5.2 tool guardrail halt assistant

**机制启用状态：⚠️ 条件启用（默认硬停关闭）。** 工具 guardrail 默认只发 warning；只有 `tool_loop_guardrails.hard_stop_enabled=true` 且控制器返回 `block`/`halt` 时，才补 guardrail assistant。未启用硬停或决策为 allow/warn 时不会触发。

**WebUI 对齐状态：✅ 已对齐。** guardrail 说明作为真实 assistant 终态保留，并与工具拦截记录一起展示。

**上游持久化规则：✅ 一致。** guardrail halt assistant 与对应工具轨迹均保留，Fork 未改为 scaffold。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"删除生产环境里 30 天前的订单数据。","_turn_key":"turn:16"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delete","function":{"name":"delete_orders","arguments":"{\"age_days\":30}"}}]},
  {"role":"tool","tool_call_id":"call_delete","content":"操作被安全规则拦截：缺少生产删除授权。"},
  {"role":"assistant","content":"我没有删除任何数据。生产环境删除需要额外授权。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"删除生产环境里 30 天前的订单数据。","_turn_key":"turn:16"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delete","function":{"name":"delete_orders","arguments":"{\"age_days\":30}"}}]},
  {"role":"tool","tool_call_id":"call_delete","content":"操作被安全规则拦截：缺少生产删除授权。"},
  {"role":"assistant","content":"我没有删除任何数据。生产环境删除需要额外授权。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"删除生产环境里 30 天前的订单数据。","_turn_key":"turn:16"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_delete","function":{"name":"delete_orders","arguments":"{\"age_days\":30}"}}]},
  {"role":"tool","tool_call_id":"call_delete","content":"操作被安全规则拦截：缺少生产删除授权。"},
  {"role":"assistant","content":"我没有删除任何数据。生产环境删除需要额外授权。"}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认具备（条件触发）。** Agent 本地处理失败或接近预算上限、需要向用户说明退出原因时补真实 assistant；正常成功路径不触发。

**WebUI 对齐状态：✅ 已对齐。** 本地错误说明按真实 assistant 终态投影，不额外制造用户气泡。

**上游持久化规则：✅ 一致。** 本地处理错误和接近预算上限错误均作为真实 assistant 行保留。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"读取 customer-data.csv 并总结异常数据。","_turn_key":"turn:17"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_csv","function":{"name":"read_file","arguments":"{\"path\":\"customer-data.csv\"}"}}]},
  {"role":"tool","tool_call_id":"call_csv","content":"错误：CSV 第 18 行字段数量不一致。"},
  {"role":"assistant","content":"I apologize, but I encountered an error while processing the model response: CSV 第 18 行字段数量不一致。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"读取 customer-data.csv 并总结异常数据。","_turn_key":"turn:17"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_csv","function":{"name":"read_file","arguments":"{\"path\":\"customer-data.csv\"}"}}]},
  {"role":"tool","tool_call_id":"call_csv","content":"错误：CSV 第 18 行字段数量不一致。"},
  {"role":"assistant","content":"I apologize, but I encountered an error while processing the model response: CSV 第 18 行字段数量不一致。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"读取 customer-data.csv 并总结异常数据。","_turn_key":"turn:17"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_csv","function":{"name":"read_file","arguments":"{\"path\":\"customer-data.csv\"}"}}]},
  {"role":"tool","tool_call_id":"call_csv","content":"错误：CSV 第 18 行字段数量不一致。"},
  {"role":"assistant","content":"I apologize, but I encountered an error while processing the model response: CSV 第 18 行字段数量不一致。"}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认具备（条件触发）。** 当前源码的专用路径是“启用工具时 Ollama `ollama_num_ctx` 低于 Hermes 最低上下文要求”；此时在发起 provider 请求前补真实 assistant。其它 provider 的一般上下文超限走压缩/错误分支，不应套用本小类文案。

**WebUI 对齐状态：✅ 已对齐。** runtime context 不足说明作为真实失败回答保留并可从历史重载。

**上游持久化规则：✅ 一致。** runtime context 不足 assistant 是真实 durable 终态，Fork 未改变其写入规则。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"分析整个项目，找出登录链路的性能瓶颈。","_turn_key":"turn:18"},
  {"role":"assistant","content":"Ollama loaded `<model>` with only <runtime_ctx> tokens of runtime context, but Hermes needs at least <minimum_ctx> tokens for reliable tool use.\n\nIncrease the Ollama context for this model and restart/reload the model before trying again. A known-good starting point is 65,536 tokens. In Hermes config, set `model.ollama_num_ctx: 65536` (and `model.context_length: 65536` if you also override the displayed model context). If you manage the model through an Ollama Modelfile, set `PARAMETER num_ctx 65536` there instead."}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"分析整个项目，找出登录链路的性能瓶颈。","_turn_key":"turn:18"},
  {"role":"assistant","content":"Ollama loaded `<model>` with only <runtime_ctx> tokens of runtime context, but Hermes needs at least <minimum_ctx> tokens for reliable tool use.\n\nIncrease the Ollama context for this model and restart/reload the model before trying again. A known-good starting point is 65,536 tokens. In Hermes config, set `model.ollama_num_ctx: 65536` (and `model.context_length: 65536` if you also override the displayed model context). If you manage the model through an Ollama Modelfile, set `PARAMETER num_ctx 65536` there instead."}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"分析整个项目，找出登录链路的性能瓶颈。","_turn_key":"turn:18"},
  {"role":"assistant","content":"Ollama loaded `<model>` with only <runtime_ctx> tokens of runtime context, but Hermes needs at least <minimum_ctx> tokens for reliable tool use.\n\nIncrease the Ollama context for this model and restart/reload the model before trying again. A known-good starting point is 65,536 tokens. In Hermes config, set `model.ollama_num_ctx: 65536` (and `model.context_length: 65536` if you also override the displayed model context). If you manage the model through an Ollama Modelfile, set `PARAMETER num_ctx 65536` there instead."}
]
```

**SSE 实时与 `done`**

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

**机制启用状态：✅ 默认执行（条件触发）。** 每次 turn finalizer 都会检查收尾；仅当已有 `final_response` 但消息列表没有对应真实 assistant 行时补写，已有匹配行则不重复追加。

**WebUI 对齐状态：✅ 已对齐。** done 收口读取 finalizer 补写的 assistant，并避免重放已预览正文。

**上游持久化规则：✅ 一致。** finalizer 仅在缺失最终回答时补写真实 assistant；Fork 未改变该 durable 兜底。

Agent 运行时 `messages`（finalizer 补写前）：

```json
[
  {"role":"user","content":"运行发布前检查并告诉我结果。","_turn_key":"turn:19"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_release","function":{"name":"release_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_release","content":"12 项检查全部通过。"}
]
```

Agent durable transcript（finalizer 补写后）：

```json
[
  {"role":"user","content":"运行发布前检查并告诉我结果。","_turn_key":"turn:19"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_release","function":{"name":"release_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_release","content":"12 项检查全部通过。"},
  {"role":"assistant","content":"发布前检查完成，12 项全部通过。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"运行发布前检查并告诉我结果。","_turn_key":"turn:19"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_release","function":{"name":"release_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_release","content":"12 项检查全部通过。"},
  {"role":"assistant","content":"发布前检查完成，12 项全部通过。"}
]
```

最终回答只显示一次。

**SSE 实时与 `done`**

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

### 6.1 todo snapshot：Fork 固化为独立的 context-anchor user

**机制启用状态：✅ 随压缩流程默认具备（条件触发）。** 压缩构造上下文且 todo store 有可注入 snapshot 时生成；没有待办内容或未发生压缩时不生成该 anchor。

**WebUI 对齐状态：✅ 已与当前 Fork 对齐。** 独立 todo anchor 被隐藏；这不是上游可合并真实 user 的逐行持久化形状。

**上游持久化规则：❌ 不一致。** Fork 强制持久化为独立 `context_anchor/todo_snapshot`；上游可将 snapshot 合并进真实 user 行。

#### 上游原有行为

上游先调用 `agent._todo_store.format_for_injection()` 生成当前待办快照，再检查压缩结果
`compressed` 的最后一行。只有最后一行同时满足以下条件时，才会合并：

1. `role="user"`；
2. 该行通过 `_is_real_user_message()` 判定，亦即不是 continuation、压缩摘要、旧 todo
snapshot 或其它 synthetic user scaffold；
3. 该行不是只剩历史 snapshot 的空壳。

满足条件时，上游会先从该真实 user 的 `content` 移除以前拼入的 todo block，再把最新
snapshot 追加到同一 `content`。原有的 `_turn_key`、附件、来源身份和 `role` 都保持不变，
不会产生新的 durable message，也不会给整条真实 user 打 synthetic 标记。

若尾部不是真实 user，上游不会把 snapshot 合入控制行，以免把内部材料误判为人类意图：

- 若尾部只是上一次独立 snapshot，则原地替换为最新 snapshot，避免连续压缩时累积旧待办；
- 其它不能安全合并的情况，才追加独立 `role="user"`、
  `_todo_snapshot_synthetic=true` 的 legacy synthetic row。

这也是上游展示语义的限制：snapshot 被合并进真实 user 后，展示层不能在不改写真实用户
内容的前提下单独隐藏它。Fork 改为独立 `context_anchor`，正是为了同时保留模型上下文、
真实 user 的身份字段和可隐藏的 WebUI 投影。

上游 Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。\n\n待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

上游 Agent durable transcript：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。\n\n待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

上游 WebUI 展示投影：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。\n\n待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

#### 当前 Fork 行为

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"user","content":"待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"todo_snapshot","_todo_snapshot_synthetic":true},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"user","content":"待办：1. 修复登录超时；2. 补回归测试。当前第 1 项已完成。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"todo_snapshot","_todo_snapshot_synthetic":true},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"先修复登录超时，再补回归测试。","_turn_key":"turn:20"},
  {"role":"assistant","content":"我先处理登录超时。"},
  {"role":"assistant","content":"登录超时已修复，现在开始补回归测试。"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  todo snapshot 是压缩/上下文重建数据，不会产生 user SSE 事件，也不是实时聊天气泡。
  本轮真正的工具、thinking 和正文仍按各自 SSE 事件发送。

Agent durable：
  context_anchor 可以写入 Agent state.db，并保留 hermes_message_class / scaffold_kind。

done / 历史：
  WebUI projection 只过滤 todo context_anchor，不会仅因其前后出现而删除真实 assistant。
  本例的两条 assistant 都没有候选替换或 length 续写标记，因此都会保留。
```

实现位置：`hermes-agent/agent/conversation_compression.py`

以下描述的是当前 Fork 的 canonical transcript。上下文压缩时，Fork 会把 todo 快照
作为独立的 `role="user"`、`context_anchor/todo_snapshot` 注入。

这不是上游的逐行持久化规则。上游会优先将 snapshot 合并进尾部真实 user；只有无法
安全合并时，才追加带 `_todo_snapshot_synthetic` 的独立 user。上游因此可能改写真实
user 的 content，而不会产生单独 durable 行。

Fork 始终保留独立 anchor，避免把 model-only 待办混入真实 user 的身份、turn key 与
展示内容。两边保留的待办信息相同，但 durable 行数、内容归属和重放形状不同。

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

### 6.2 压缩摘要：Fork 实现为独立 context-anchor 恢复材料

**机制启用状态：✅ 默认启用（条件触发）。** `compression.enabled=true`（默认）且上下文超过阈值、或用户显式执行压缩时生成摘要；关闭压缩或未达到触发条件时不生成。摘要模型失败时，默认 `abort_on_summary_failure=false` 会改用确定性 fallback；设为 `true` 才会保留原消息并中止本次压缩。

**WebUI 对齐状态：✅ 已与当前 Fork 对齐。** 独立 summary anchor 被隐藏并可 replay；这与上游冲突时合入 tail 的形状不同。

**上游持久化规则：❌ 不一致。** Fork 强制持久化为独立 `context_anchor/compaction_summary`；上游在角色冲突时可将摘要合入 tail。

压缩器生成的 `[CONTEXT COMPACTION — REFERENCE ONLY]` 摘要用于恢复模型上下文，
不是一次新的用户提交。以下是 Fork 的持久化契约，不是对上游现状的描述。

上游在摘要角色会同时撞上 head/tail 的交替约束时，会把摘要合入第一个 tail message。
这会改写该行内容，并可能使摘要与真实 user 或 assistant 共用同一 durable row。

Fork 始终写入独立的 `context_anchor/compaction_summary`。连续 user 只在发送给 provider 的
临时副本中合并，绝不回写 state.db 或 canonical transcript。因此 Fork 的 durable 行数、
角色相邻关系和 replay 形状与上游不同。

#### 上游原有行为

上游压缩器先按受保护 head 与 tail 的 template-visible role 选择 summary 的角色：若只会和
一侧相邻消息冲突，会翻转 summary 为 `user` 或 `assistant`；若两个角色都会违反交替规则，
才进入 merge-into-tail 分支。该分支不会新增 summary row，而是选择第一个 tail carrier：

1. 保留 carrier 原来的内容；
2. 加入 `[PRIOR CONTEXT ...]` 与 `[END OF PRIOR CONTEXT ...]` 分隔符；
3. 在分隔符后写入 summary 与 `--- END OF CONTEXT SUMMARY ... ---`；
4. 把 `_compressed_summary=true` 写到这个被改写的 tail 行，并使旧 `api_content` sidecar
   失效。

carrier 可以是 user、assistant，或严格模板不计入交替的 tool-flow 行；因此上游的摘要
不一定有独立角色、独立 message ID 或独立展示归属。若 summary 可以独立插入，上游才创建
带 `_compressed_summary=true` 的 standalone summary row。Fork 不在 canonical transcript
执行上述 tail 改写，始终保留独立 anchor；连续角色只在 provider 请求副本中临时处理。

上游 Agent 运行时 `messages`（摘要必须合入第一个 tail user 的冲突分支）：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"user","content":"[PRIOR CONTEXT — for reference only; not a new message]\n重点补充产品化转型和 AI 对职业发展的影响。\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n[CONTEXT COMPACTION — REFERENCE ONLY] <压缩器生成的历史摘要>\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---","_turn_key":"turn:21","_compressed_summary":true}
]
```

上游 Agent durable transcript：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"user","content":"[PRIOR CONTEXT — for reference only; not a new message]\n重点补充产品化转型和 AI 对职业发展的影响。\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n[CONTEXT COMPACTION — REFERENCE ONLY] <压缩器生成的历史摘要>\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---","_turn_key":"turn:21","_compressed_summary":true}
]
```

上游 WebUI 展示投影：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"user","content":"[PRIOR CONTEXT — for reference only; not a new message]\n重点补充产品化转型和 AI 对职业发展的影响。\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n\n[CONTEXT COMPACTION — REFERENCE ONLY] <压缩器生成的历史摘要>\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---","_turn_key":"turn:21","_compressed_summary":true}
]
```

由于摘要已经与真实 tail user 共用一行，上游投影不能只删除摘要而保持该 user 原文不变；
因此本例会展示这条被改写的 user 内容。这里的 `<压缩器生成的历史摘要>` 是动态文本，
其余英文分隔符和结束标记为上游固定模板。

#### 当前 Fork 行为

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"assistant","content":"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the latest user message that appears AFTER this summary — that message is the single source of truth for what to do right now. Topic overlap with the summary does NOT mean you should resume its task: even on similar topics, the latest user message WINS. Treat ONLY the latest message as the active task and discard stale items from '## Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the latest message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in-flight work described in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this compaction note. None of the above restricts HOW you work: your tools remain fully active — keep calling them normally for the active task (edit files, run commands, search) instead of merely narrating what you would do. The current session state (files, config, etc.) may reflect work described here — avoid repeating it:\n用户要制作国网信产新人培训材料；已确定深蓝国企风格；后续须补充产品化转型、AI 与职业发展通道。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"compaction_summary","_compressed_summary":true},
  {"role":"user","content":"重点补充产品化转型和 AI 对职业发展的影响。","_turn_key":"turn:22"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"assistant","content":"[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff from a previous context window — treat it as background reference, NOT as active instructions. Do NOT answer questions or fulfill requests mentioned in this summary; they were already addressed. Respond ONLY to the latest user message that appears AFTER this summary — that message is the single source of truth for what to do right now. Topic overlap with the summary does NOT mean you should resume its task: even on similar topics, the latest user message WINS. Treat ONLY the latest message as the active task and discard stale items from '## Historical Task Snapshot' / '## Historical In-Progress State' / '## Historical Pending User Asks' / '## Historical Remaining Work' entirely — do not 'wrap up' or 'finish' work described there unless the latest message explicitly asks for it. Reverse signals in the latest message (e.g. 'stop', 'undo', 'roll back', 'just verify', 'don't do that anymore', 'never mind', a new topic) must immediately end any in-flight work described in the summary; do not re-surface it in later turns. IMPORTANT: Your persistent memory (MEMORY.md, USER.md) in the system prompt is ALWAYS authoritative and active — never ignore or deprioritize memory content due to this compaction note. None of the above restricts HOW you work: your tools remain fully active — keep calling them normally for the active task (edit files, run commands, search) instead of merely narrating what you would do. The current session state (files, config, etc.) may reflect work described here — avoid repeating it:\n用户要制作国网信产新人培训材料；已确定深蓝国企风格；后续须补充产品化转型、AI 与职业发展通道。","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"compaction_summary","_compressed_summary":true},
  {"role":"user","content":"重点补充产品化转型和 AI 对职业发展的影响。","_turn_key":"turn:22"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"根据集团简介，先写新人培训 PPT 讲稿，再制作深蓝风格 HTML 演示稿。","_turn_key":"turn:21"},
  {"role":"assistant","content":"我会先整理讲稿结构，再生成 HTML。"},
  {"role":"user","content":"重点补充产品化转型和 AI 对职业发展的影响。","_turn_key":"turn:22"}
]
```

本例的 `turn:22` 是压缩前已提交的最新真实 user turn，不是 summary anchor。WebUI 隐藏
中间的 `compaction_summary` 后，仍应依时间顺序展示 `turn:21` 与 `turn:22` 两条用户消息。

本例选择 assistant-role 摘要，使其与紧随的真实 user 自然交替；它不是规定摘要永远为
assistant。摘要角色应由压缩边界决定。若目标态确实需要 `user` 摘要紧邻真实 user，
canonical transcript 可以保留两行，严格 provider 只在临时副本合并，不能回写持久化状态。

**实现状态：✅ 当前 `fix-artifact` 本地补丁已验证。** 该实现产生本例的
`context_anchor/compaction_summary`，同时保留 `_compressed_summary=True` 作为
压缩器兼容标记。上游当前仍可能把摘要合入真实 tail；该补丁不追溯重写这些历史行。

**SSE 实时与 `done`**

```text
SSE 实时：
  压缩摘要不是新的实时用户提交，不会发送 user SSE，也不生成用户气泡。

Agent durable：
  摘要作为 context_anchor 写入 state.db；真实的最后一条 user 保持原内容和身份字段。

模型请求：
  若 provider 不接受相邻 user，Agent 只在临时 API 副本中合并摘要和真实请求。
  该临时合并不得回写 state.db、api_content sidecar 或 WebUI transcript。
```

压缩摘要必须保留在 Agent `state.db`，否则重启后模型会失去压缩后的恢复材料；
它也必须由 WebUI 投影隐藏，不能取得真实 user 的 `_turn_key`、来源身份或
turn 归属。

本地实现统一写入 `_hermes_message_class="context_anchor"` 和
`_hermes_scaffold_kind="compaction_summary"`；`_compressed_summary=True` 只作为压缩器内部与
旧消费者的兼容标记，不再是持久化或展示分类的唯一依据。

若摘要原本会合并进真实 tail user，不能给合并后的整条 tail 打
`context_anchor` 标记；那会隐藏真实请求。应保留真实 tail 原样，并将摘要作为
独立的 `compaction_summary` anchor 表示，具体拆分策略见
[`压缩摘要上下文锚点修复方案`](../plans/压缩摘要上下文锚点修复方案.md)。

### 6.3 压缩后没有真实 user turn：Fork 补 context-anchor user

**机制启用状态：✅ 随压缩流程默认具备（条件触发）。** 压缩完成后如果保留下来的消息中没有真实 user turn，才补 `compression_no_user_anchor`；存在真实 user 时不补。

**WebUI 对齐状态：✅ 已与当前 Fork 对齐。** fallback anchor 被隐藏；上游的同文本普通 durable user 不适用该投影。

**上游持久化规则：⚠️ 内容与时机一致，元数据与展示语义不同。** 两侧都保留 fallback user；上游为普通 durable user，Fork 标记为 `context_anchor` 并由 WebUI 隐藏。

#### 当前 Fork 行为

Agent 运行时 `messages`：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"user","content":"Continue from the compressed conversation context above. This marker exists because no human user turn was available.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"compression_no_user_anchor"},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

Agent durable transcript：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"user","content":"Continue from the compressed conversation context above. This marker exists because no human user turn was available.","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"compression_no_user_anchor"},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

**SSE 实时与 `done`**

```text
这是压缩完成后为下一次模型调用准备的 user-role anchor，不是当前 SSE 中新来的用户输入。
它不会发 user SSE；即使被 Agent 持久化，done 和重新加载历史都会按 context_anchor 过滤。
前一条只带 `_compressed_summary` 的 assistant 不属于 control message，仍会保留在展示投影中。
```

实现位置：`hermes-agent/agent/conversation_compression.py`

如果压缩结果中没有真实 human user turn，Agent 会尽量把最近的一条真实 user 问题重新插入压缩结果；如果连这个都没有，就补一个 fallback user marker。

上游会把 fallback 作为普通 durable user 写入。Fork 保留相同文本与追加时机，但标记为
`context_anchor/compression_no_user_anchor`，并由 WebUI 隐藏。这里是 metadata 与展示语义
差异，不是文本内容差异。

#### 上游原有行为

上游先在压缩结果中寻找 `_is_real_user_message()` 为真的 user；若找到，认为压缩结果已有
可行动的人类意图，不追加 fallback。若压缩结果没有，它会从压缩前的原始 messages 倒序寻找
最近一条真实 user，并以新的 message copy 插入压缩结果，优先恢复真实问题而不是创建标记。

只有两处都没有真实 user 时，上游才追加本节的 fallback：它只有 `role="user"` 与固定
`content`，没有 `_hermes_message_class`、`_hermes_scaffold_kind` 或 synthetic legacy flag。
也就是说，在上游 durable transcript 中它是普通 user row；下游若只看角色或没有内容前缀
识别，就无法像 Fork 一样从结构化元数据判断它只是恢复锚点。Fork 保留同一正文和触发时机，
但把它写为独立 `context_anchor/compression_no_user_anchor`，从而允许 WebUI 隐藏该行而不影响
真实 user 的身份与 turn 归属。

典型 fallback 文案是：

```text
Continue from the compressed conversation context above. This marker exists because no human user turn was available.
```

这类消息的目的不是给用户看，而是维持压缩后上下文对后续模型调用仍然有可行动的人类锚点。

上游 Agent 运行时 `messages`：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"user","content":"Continue from the compressed conversation context above. This marker exists because no human user turn was available."},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

上游 Agent durable transcript：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"user","content":"Continue from the compressed conversation context above. This marker exists because no human user turn was available."},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

上游 WebUI 展示投影：

```json
[
  {"role":"assistant","content":"用户正在分析登录性能，数据库查询占总耗时的 72%。","_compressed_summary":true},
  {"role":"user","content":"Continue from the compressed conversation context above. This marker exists because no human user turn was available."},
  {"role":"assistant","content":"主要瓶颈是数据库查询，下一步应检查索引和 N+1 查询。"}
]
```

上游 fallback 没有结构化语义字段，所以当前 Fork WebUI 无法把它识别为可隐藏锚点；按普通
durable user 的默认投影会保留该行。

### 6.4 哪些 `role="user"` 其实不算真实用户输入

**机制启用状态：✅ 默认分类规则。** 每次 turn 绑定、压缩、manifest 和 WebUI 投影都按结构化语义识别真实 user；没有单独的启停开关。

**WebUI 对齐状态：✅ 已对齐。** WebUI 按结构化语义区分真实 user、context anchor 与 internal scaffold，不只依据 `role`。

**上游持久化规则：⚠️ 基线一致，Fork 有扩展。** 上游会排除既有 synthetic user；Fork 额外以 `context_anchor` 为权威标记排除其新增的控制行。

实现位置：`hermes-agent/agent/conversation_compression.py`

压缩逻辑明确区分“真实 user turn”和“仅仅是 user-role scaffolding”。以下几类会被排除出真实 human intent：

- `_todo_snapshot_synthetic`
- `_empty_recovery_synthetic`
- `_verification_stop_synthetic`（仅 verification user nudge）
- `_pre_verify_synthetic`（仅 pre_verify user nudge）
- context summary / synthetic prefix 形式的 user-role scaffold

本 Fork 还会以 `_hermes_message_class` 为准，把 `internal_scaffold` 与
`context_anchor` 排除在真实 user intent 之外。这里的“上游没有这对字段”特指
`upstream/main` 基线；当前 `fix-artifact` Agent 源码已经引入同名结构化字段，不能再把
它描述成当前运行源码缺失的能力。

这说明在 Hermes Agent 的语义里：

- `role="user"` 不等于“就是人类输入”
- 判断是否为真实用户意图，必须结合 flag 和内容语义

本节只定义压缩器的判定入口，不重复各消息的生命周期示例：todo snapshot、压缩摘要和
no-user fallback 分别见 6.1、6.2、6.3；WebUI 对 `internal_scaffold` 与 `context_anchor`
的统一过滤规则见 8.1、8.2。

### 6.5 手动局部压缩：head/tail seam bridge 与工具配对清理（未实现）

**机制启用状态：⚠️ 条件启用。** 用户执行 `/compress here [N]` 或 Gateway 等价命令时，
Agent 只压缩 head、原样保留最近 tail，再把两段重新拼接。若压缩边界造成相邻同角色，
字符串内容会合并；多模态内容无法安全合并时会插入最小 bridge turn。压缩后还会清理孤立
tool result，并从 assistant 中剥离没有结果的 tool call。

**WebUI 对齐状态：⚠️ 未专门覆盖。** 这是 CLI/Gateway 的压缩操作，不是普通聊天 SSE；
WebUI 读取压缩后的 durable transcript，但目前没有专门展示 bridge、tool-pair 清理或
`(tool call removed)` 的投影说明。

**上游持久化规则：✅ 一致。** seam 合并、bridge、孤立工具清理发生在压缩结果写入新会话
或原地压缩前；它们属于压缩后的 canonical transcript，不是普通用户输入。Fork 当前
未改变这条 Agent 规则。

实现位置：`hermes-agent/hermes_cli/partial_compress.py`、
`hermes-agent/agent/context_compressor.py`、`hermes-agent/cli.py`、
`hermes-agent/gateway/slash_commands.py`。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"先分析迁移风险。","_turn_key":"turn:34"},
  {"role":"assistant","content":"风险主要在双写一致性。","tool_calls":[{"id":"call_old","function":{"name":"inspect_schema","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_old","content":"旧工具结果未被保留。"},
  {"role":"user","content":"继续给出回滚方案。","_turn_key":"turn:35"},
  {"role":"assistant","content":"回滚方案需要先暂停双写。"}
]
```

Agent durable transcript（`/compress here 1` 重新拼接后）：

```json
[
  {"role":"user","content":"[CONTEXT COMPACTION — REFERENCE ONLY] 迁移分析已压缩。\n\n--- END OF CONTEXT SUMMARY ---","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"compaction_summary","_compressed_summary":true},
  {"role":"assistant","content":"回滚方案需要先暂停双写。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"assistant","content":"回滚方案需要先暂停双写。"}
]
```

上例中，如果压缩 head 末尾与 tail 开头都是 user/assistant，Agent 会在 provider 规则需要
时合并字符串；如果 tool result 没有 surviving assistant tool call，则删除该 tool result；
如果 assistant 的所有 tool call 都失去结果，则移除 `tool_calls`，必要时写入
`(tool call removed)` 保证请求仍合法。这个过程不会创建新的真实 user turn。

## 7. 还有六类值得单独提到

### 7.1 invalid tool JSON 恢复：补 assistant + tool

**机制启用状态：✅ 默认协议恢复路径（条件触发）。** 模型声明了工具但参数无法解析为合法 JSON 时触发；前两次普通格式错误只重试、不改写 `messages`，第 3 次才补 assistant + tool error。若参数是输出截断造成的残缺 JSON，则直接按截断 partial 失败返回，不走这条恢复 pair；参数合法或没有工具调用时不触发。

**WebUI 对齐状态：⚠️ 部分对齐。** 真实 assistant/tool 轨迹会保留；参数错误专用工具卡片尚未完成验证。

**上游持久化规则：✅ 一致。** invalid JSON 恢复产生的 assistant + tool 是可恢复的真实 durable 轨迹，不是 scaffold。

下面的三段式示例展示的是普通 invalid-JSON 连续重试达到第 3 次后的恢复出口；前两次
模型返回坏参数时，源码只重新请求，不会在 `messages` 中留下 assistant/tool 行。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:23"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_1","function":{"name":"deployment_check","arguments":"{bad-json"}}]},
  {"role":"tool","tool_call_id":"call_deploy_1","content":"Error: Invalid JSON arguments. <error>. For tools with no required parameters, use an empty object: {}. Please retry with valid JSON."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_2","function":{"name":"deployment_check","arguments":"{\"environment\":\"staging\"}"}}]},
  {"role":"tool","tool_call_id":"call_deploy_2","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:23"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_1","function":{"name":"deployment_check","arguments":"{bad-json"}}]},
  {"role":"tool","tool_call_id":"call_deploy_1","content":"Error: Invalid JSON arguments. <error>. For tools with no required parameters, use an empty object: {}. Please retry with valid JSON."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_2","function":{"name":"deployment_check","arguments":"{\"environment\":\"staging\"}"}}]},
  {"role":"tool","tool_call_id":"call_deploy_2","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:23"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_1","function":{"name":"deployment_check","arguments":"{bad-json"}}]},
  {"role":"tool","tool_call_id":"call_deploy_1","content":"Error: Invalid JSON arguments. <error>. For tools with no required parameters, use an empty object: {}. Please retry with valid JSON."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy_2","function":{"name":"deployment_check","arguments":"{\"environment\":\"staging\"}"}}]},
  {"role":"tool","tool_call_id":"call_deploy_2","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

**SSE 实时与 `done`**

```text
SSE 实时：
  原始 assistant 的 tool call 通过 tool 事件表现，坏 JSON 的说明是 tool result，不是 user nudge。
  因此用户看到的是失败工具卡片、重试工具卡片和后续 token，而不是 Agent 自己补的一条用户消息。

done / 历史：
  原始 assistant(tool_calls)、工具错误/成功结果和最终回答都是可恢复业务轨迹，按工具语义
  展示；这里没有 internal_scaffold assistant + user pair。
```

实现位置：`hermes-agent/agent/conversation_loop.py`

当模型生成了普通格式错误的 tool call JSON，前两次只重试；第 3 次仍失败时，Agent 不会补 user，而是：

1. 先补一条带坏 tool_calls 的 assistant
2. 再补若干 tool error result

这是为了让模型下一轮直接基于工具错误自我修复，而不是把恢复指令伪装成用户消息。若检测到参数是被输出长度截断，Agent 会直接返回 `Response truncated due to output length limit` 并持久化当前 partial 状态，不伪造 invalid-JSON 工具结果。

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

### 7.2 unknown tool name 恢复：补 assistant + tool error result

**机制启用状态：✅ 默认协议恢复路径（条件触发）。** 模型请求的工具名不在当前有效工具集合时触发；工具名合法时不补 unknown-tool error result。

**WebUI 对齐状态：⚠️ 部分对齐。** 错误与跳过结果会保留为工具轨迹；未知工具名专用卡片尚未完成验证。

**上游持久化规则：✅ 一致。** unknown-tool assistant + error result 是可恢复的真实 durable 轨迹，不是 scaffold。

实现位置：`hermes-agent/agent/conversation_loop.py`

模型调用了不存在的工具时，Agent 会保留原始 assistant 的 `tool_calls`，再为该批次
每个调用补 `tool` result，让模型在下一轮根据结构化错误自行改正。

```text
Tool '<tool_name>' does not exist. Available tools: <available tools>
```

同一批次里名称合法的调用不会被执行，而是收到如下结果，要求整体重试：

```text
Skipped: another tool call in this turn used an invalid name. Please retry this tool call.
```

这与 invalid tool JSON 恢复相似，但触发条件是**工具名不存在**，不是参数解析失败。
这些 assistant / tool 行不带 `internal_scaffold`，应作为可恢复的工具失败轨迹保留。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:24"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_unknown","function":{"name":"check_stage_environment","arguments":"{}"}},{"id":"call_config","function":{"name":"read_deploy_config","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_unknown","content":"Tool 'check_stage_environment' does not exist. Available tools: deployment_check, read_deploy_config"},
  {"role":"tool","tool_call_id":"call_config","content":"Skipped: another tool call in this turn used an invalid name. Please retry this tool call."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_deploy","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:24"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_unknown","function":{"name":"check_stage_environment","arguments":"{}"}},{"id":"call_config","function":{"name":"read_deploy_config","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_unknown","content":"Tool 'check_stage_environment' does not exist. Available tools: deployment_check, read_deploy_config"},
  {"role":"tool","tool_call_id":"call_config","content":"Skipped: another tool call in this turn used an invalid name. Please retry this tool call."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_deploy","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:24"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_unknown","function":{"name":"check_stage_environment","arguments":"{}"}},{"id":"call_config","function":{"name":"read_deploy_config","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_unknown","content":"Tool 'check_stage_environment' does not exist. Available tools: deployment_check, read_deploy_config"},
  {"role":"tool","tool_call_id":"call_config","content":"Skipped: another tool call in this turn used an invalid name. Please retry this tool call."},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_deploy","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_deploy","content":"检查通过，可以部署。"},
  {"role":"assistant","content":"staging 环境部署检查通过，可以部署。"}
]
```

**SSE 实时与 `done`**

```text
未知工具名和跳过结果均是 tool 事件，不产生 user SSE，也不会形成内部用户气泡。
done 与历史保留失败和重试的工具轨迹，便于解释为什么首次调用没有执行。
```

### 7.3 历史中的损坏 tool arguments：修复参数并补 tool marker

**机制启用状态：✅ 默认历史修复路径（条件触发）。** Agent 重放或继续历史消息时发现已有 tool-call 参数损坏，才修复参数并补 marker；新生成且合法的工具调用不触发。

**WebUI 对齐状态：⚠️ 部分对齐。** marker 不会被当作用户消息；历史参数损坏的专用工具呈现尚未完成验证。

**上游持久化规则：✅ 一致。** 修复后的 tool arguments 和补写的 marker 保留为 durable 工具轨迹。

实现位置：`hermes-agent/agent/agent_runtime_helpers.py`、`hermes-agent/agent/conversation_loop.py`

Agent 在下一次请求前会扫描已有 transcript。若 assistant 的 `tool_calls[].function.arguments`
已经损坏到无法解析，会将参数改为 `{}`，并确保紧随其后的对应 tool result 带有损坏标记。

若该 tool result 缺失，Agent 会插入一条；若已经存在，则在其内容前追加 marker。
这样严格 provider 会收到合法的 assistant / tool 对，而不是把历史损坏误判成新的用户输入。

这是一条原地的历史修复路径，不是模型生成的新工具调用，也不是 `internal_scaffold`。
展示层应按 tool 语义呈现该错误结果，并保留其“历史参数已损坏”的上下文。

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:25"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_history","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_history","content":"[hermes-agent: tool call arguments were corrupted in this session and have been dropped to keep the conversation alive. See issue #15236.]"},
  {"role":"user","content":"继续处理上一次的 staging 部署检查。","_turn_key":"turn:26"},
  {"role":"assistant","content":"基于修复后的工具轨迹继续给出下一步。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:25"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_history","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_history","content":"[hermes-agent: tool call arguments were corrupted in this session and have been dropped to keep the conversation alive. See issue #15236.]"},
  {"role":"user","content":"继续处理上一次的 staging 部署检查。","_turn_key":"turn:26"},
  {"role":"assistant","content":"基于修复后的工具轨迹继续给出下一步。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查 staging 环境能不能部署。","_turn_key":"turn:25"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_history","function":{"name":"deployment_check","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_history","content":"[hermes-agent: tool call arguments were corrupted in this session and have been dropped to keep the conversation alive. See issue #15236.]"},
  {"role":"user","content":"继续处理上一次的 staging 部署检查。","_turn_key":"turn:26"},
  {"role":"assistant","content":"基于修复后的工具轨迹继续给出下一步。"}
]
```

**SSE 实时与 `done`**

```text
这是恢复前的历史修复，不是浏览器在当前轮新发送的 user 消息。
若工具 marker 被展示，应保持工具卡片语义；它不能被投影为用户的补充请求。
```

### 7.4 MoA aggregator guidance：必要时补一条 user

**机制启用状态：⚠️ 条件启用。** 只有启用 MoA preset、实际发生 reference/aggregator 调用且聚合器需要 guidance 时才存在；`moa.save_traces` 是独立的审计开关，默认关闭，不控制 guidance 本身。

**WebUI 对齐状态：— 无需接入。** guidance 只存在于聚合器请求副本，不进入主 WebUI transcript。

**上游持久化规则：✅ 一致。** guidance 仅存在于聚合器的 provider 请求副本，不进入主会话 durable transcript。

Agent 运行时 `messages`（主会话）：

```json
[
  {"role":"user","content":"综合几个模型的意见，告诉我数据库迁移该选方案 A 还是方案 B。","_turn_key":"turn:27"},
  {"role":"assistant","content":"建议选择方案 B，但需要先补充回滚演练来控制风险。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"综合几个模型的意见，告诉我数据库迁移该选方案 A 还是方案 B。","_turn_key":"turn:27"},
  {"role":"assistant","content":"建议选择方案 B，但需要先补充回滚演练来控制风险。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"综合几个模型的意见，告诉我数据库迁移该选方案 A 还是方案 B。","_turn_key":"turn:27"},
  {"role":"assistant","content":"建议选择方案 B，但需要先补充回滚演练来控制风险。"}
]
```

MoA 聚合器的 provider 请求副本另有一条临时 guidance user，不属于主会话数组，也不进入 durable transcript。

**SSE 实时与 `done`**

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

### 7.5 API 请求前结构修复：`repair_message_sequence`

**机制启用状态：✅ 默认执行（条件修复）。** 每次模型请求前都会对 live `messages` 做
防御性扫描；没有结构问题时是空操作。它会合并相邻 assistant、删除没有对应
`assistant.tool_calls` 的孤立 tool、删除重复 tool result，并保留 Codex Responses 可重放的
incomplete assistant。相邻 user 不在这里合并，provider-only 副本另行处理。

**WebUI 对齐状态：⚠️ 部分覆盖。** 真实工具轨迹最终会被 WebUI 读取，但 WebUI 没有单独
记录“本次请求前 canonical messages 被原地修复”的事件；若修复删除或合并了行，展示层只能
看到修复后的结果。

**上游持久化规则：✅ 一致。** 这是 canonical `messages` 的原地结构修复，不是新 user
输入，也不是内部 scaffold。修复后的列表会参与后续 flush；数据库游标同步重算，避免因
列表缩短而漏持久化未刷新的 assistant/tool 行。

实现位置：`hermes-agent/agent/agent_runtime_helpers.py`、
`hermes-agent/agent/conversation_loop.py`。

Agent 运行时 `messages`（请求前修复）：

```json
[
  {"role":"user","content":"检查部署状态。","_turn_key":"turn:28"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_status","function":{"name":"deployment_status","arguments":"{}"}}]},
  {"role":"assistant","content":"我继续检查部署结果。"},
  {"role":"tool","tool_call_id":"orphan_call","content":"旧会话残留结果"}
]
```

Agent durable transcript（修复后）：

```json
[
  {"role":"user","content":"检查部署状态。","_turn_key":"turn:28"},
  {"role":"assistant","content":"我继续检查部署结果。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查部署状态。","_turn_key":"turn:28"},
  {"role":"assistant","content":"我继续检查部署结果。"}
]
```

上例中，相邻 assistant 会被合并或按验证候选规则由后者替换；孤立 tool 会被删除。该逻辑
不应被 WebUI 当作“隐藏消息”实现，否则会把 Agent 需要的 tool 配对和 flush 游标一起破坏。

### 7.6 后台 memory / skill review fork：主会话之外的辅助 Agent

**机制启用状态：⚠️ 条件启用。** 默认每约 10 个用户 turn 检查一次；只有启用了 memory
或 skill 工具、达到对应 nudge interval、且本轮有正常最终回答时才启动。它在后台创建一个
review fork；同模型回放完整快照，切换 auxiliary 模型时回放压缩 digest。

**WebUI 对齐状态：✅ 无需主会话接入。** review fork 的状态、工具调用和最终文本不会进入
主 WebUI SSE、主 session messages 或普通回答气泡；用户最多看到后台完成摘要/状态通知。

**上游持久化规则：✅ 一致。** review fork 设置 `_persist_disabled=true`，不会把 harness
user prompt 或 review assistant 写入主 session durable transcript。它只允许 memory / skill
工具产生外部知识库或技能文件变更；review fork 自己也关闭压缩和周期 nudge。

实现位置：`hermes-agent/agent/turn_finalizer.py`、`hermes-agent/agent/background_review.py`、
`hermes-agent/agent/agent_init.py`。

Agent 运行时 `messages`（review fork，不是主会话）：

```json
[
  {"role":"user","content":"[Earlier conversation digest — older turns summarised to bound the review's cold-write cost on the routed aux model. Recent turns follow verbatim below.]\nUSER: 用户希望答案保持三段式。"},
  {"role":"user","content":"Review the conversation above and update the skill library. Be ACTIVE — most sessions produce at least one skill update, even if small."},
  {"role":"assistant","content":"已更新消息投影规范技能。"}
]
```

Agent durable transcript（主会话）：

```json
[
  {"role":"user","content":"请检查这次消息投影是否符合文档。","_turn_key":"turn:29"},
  {"role":"assistant","content":"消息投影已完成检查。"}
]
```

WebUI 展示投影（主会话）：

```json
[
  {"role":"user","content":"请检查这次消息投影是否符合文档。","_turn_key":"turn:29"},
  {"role":"assistant","content":"消息投影已完成检查。"}
]
```

这里的 digest 和 review prompt 只属于辅助 Agent 的请求上下文。源码中的 digest 是普通
`role=user` 文本，但由于 review fork 禁止持久化，不应被当成主会话 user，也不应添加到主会话
的 turn key 或 WebUI 投影中。

## Provider 请求合并与 durable transcript

**WebUI 当前状态：— 无需专门实现。**

这是 Agent 发送给 provider 的 `api_messages` 副本变换，不会直接成为 WebUI 的 session
或 SSE 消息。WebUI 只需继续以 canonical transcript 作为展示和持久化输入。

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

从 WebUI 或下游消费方视角，应把这些消息分成三类。是否持久化与是否展示是
两个不同维度，不能用同一个“过滤”动作代替。

### 8.1 不持久化、也不展示的 internal scaffold

**机制启用状态：✅ 默认过滤规则。** 只要消息被分类为 `internal_scaffold`，Agent durable 写入和 WebUI 投影都会按规则过滤；尚未接入的 2.5 legacy nudge 除外。

**WebUI 对齐状态：✅ 已对齐。** 已识别的 `internal_scaffold` 在实时收口与历史投影中均被过滤；仅上游的 `_dropped_toolcall_nudge` 仍见第 2.5 节的未接入说明。

**上游持久化规则：⚠️ 部分一致。** 已接入的 internal scaffold 与上游同样不持久化；`_dropped_toolcall_nudge` 尚未由 Fork 接入。

- `_empty_recovery_synthetic`
- `_empty_terminal_sentinel`
- `_thinking_prefill`
- 带 `_verification_stop_synthetic` 的 verification user nudge
- 带 `_pre_verify_synthetic` 的 pre_verify user nudge
- `_kanban_stop_synthetic`
- `_dropped_toolcall_nudge` 的 assistant + user pair（仅上游当前实现；Fork 尚未接入）

这些消息的共同点：

- 上游本来就把它们定义为临时恢复或协议脚手架
- 不代表用户真的说了什么，也不代表助手真的交付了什么
- Agent flush 和 WebUI projection 都应过滤

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"检查发布前的待办并给出结论。","_turn_key":"turn:28"},
  {"role":"assistant","content":"(empty)","_hermes_message_class":"internal_scaffold","_hermes_scaffold_kind":"empty_response_recovery","_empty_recovery_synthetic":true},
  {"role":"user","content":"You just executed tool calls but returned an empty response. Please process the tool results above and continue with the task.","_hermes_message_class":"internal_scaffold","_hermes_scaffold_kind":"empty_response_recovery","_empty_recovery_synthetic":true},
  {"role":"assistant","content":"发布前还有 1 个阻塞项：生产配置未审核。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"检查发布前的待办并给出结论。","_turn_key":"turn:28"},
  {"role":"assistant","content":"发布前还有 1 个阻塞项：生产配置未审核。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"检查发布前的待办并给出结论。","_turn_key":"turn:28"},
  {"role":"assistant","content":"发布前还有 1 个阻塞项：生产配置未审核。"}
]
```

**SSE 实时与 `done`**

这些控制行不会产生普通 user SSE，也不会进入 durable transcript。过滤必须以权威
类别或上游 legacy flag 为依据，不能扩大到同一 continuation 中的真实 assistant。

### 8.2 持久化、但不展示的 context anchor

**机制启用状态：✅ 默认分类与投影规则。** 已标记为 `context_anchor` 的消息可进入 durable transcript，但在 `done` 和历史加载投影中隐藏；没有单独的启停开关。

**WebUI 对齐状态：✅ 已与当前 Fork 对齐。** anchor 会保留给 Agent 恢复上下文，但不会成为用户气泡；todo、压缩摘要与 no-user fallback 的 durable 形状仍与上游不同，见第 6 章。

**上游持久化规则：⚠️ 部分一致。** length、ack、incomplete 和 summary-request anchor 的 durable 规则一致；todo、compaction summary 与 no-user fallback 以第 6 章的差异说明为准。

- todo snapshot、compaction summary、compression-no-user anchor
- output-length continuation user
- Codex incomplete continuation user
- Codex intermediate ack continue user
- max-iterations summary request

当前 Fork 的普通 `process_wakeup` 不在此列表中：它只有 `_source` / `_wakeup_meta`，没有
`context_anchor`，因此会按普通 user 处理。这是与 async delegation wakeup 不同的已知缺口，
见 3.5；不能把所有 server-side wakeup 都默认视为隐藏 anchor。

这些消息要保留给模型恢复或解释后续 assistant，因此会写入 Agent
`state.db`。WebUI projection 隐藏它们，不生成 user 气泡。

**三段式示例：以 length continuation 为例**

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"原始请求。","_turn_key":"turn:29"},
  {"role":"assistant","content":"已输出但被截断的真实正文。","finish_reason":"length"},
  {"role":"user","content":"[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"length_continuation"},
  {"role":"assistant","content":"续写后的真实正文。"}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"原始请求。","_turn_key":"turn:29"},
  {"role":"assistant","content":"已输出但被截断的真实正文。","finish_reason":"length"},
  {"role":"user","content":"[System: Your previous response was truncated by the output length limit. Continue exactly where you left off. Do not restart or repeat prior text. Finish the answer directly.]","_hermes_message_class":"context_anchor","_hermes_scaffold_kind":"length_continuation"},
  {"role":"assistant","content":"续写后的真实正文。"}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"原始请求。","_turn_key":"turn:29"},
  {"role":"assistant","content":"已输出但被截断的真实正文。续写后的真实正文。"}
]
```

`upstream/main` 基线没有通用的 `context_anchor` 字段。todo snapshot、compaction summary 与 no-user
fallback 在 Fork 中的独立行和隐藏投影，是 Fork 的持久化形状；其中 todo 与摘要还改变了
上游可能合并进真实 tail 的行为。

其中后四类在上游基线中是普通 durable user；当前 `fix-artifact`
本地补丁已恢复该 durable 语义，只增加 context-anchor 元数据，保持上游
正文、顺序和持久化结果不变。

### 8.3 持久化，并按一问一答规则投影的 assistant

**机制启用状态：✅ 默认投影规则。** 真实 assistant 默认保留并展示；只有命中候选替换、length 合并或其他明确投影规则时才收口为单一回答区域。

**WebUI 对齐状态：✅ 已对齐。** 真实 assistant 会按同一真实 user turn 收口；工具错误和取消结果的专用卡片覆盖范围仍以第 5、7 章的“部分对齐”说明为准。

**上游持久化规则：✅ 一致。** 本类真实 assistant 均保留为 durable 轨迹；WebUI 是否合并候选或 partial 不会回写改变 Agent transcript。

- interrupt partial assistant
- tool guardrail halt assistant
- 本地处理错误 / runtime 错误 assistant
- finalizer 兜底 assistant
- max-iterations summary answer（summary request 是 context anchor）
- output-length continuation 中已经真实输出的 partial assistant
- verify-on-stop / pre_verify 中模型已经给出的待验证 assistant 候选
- Codex intermediate ack / incomplete continuation 的 assistant

这些 assistant 保持上游 durable 语义，但“已持久化”不等于“必须各自形成一个永久
气泡”。WebUI 可在同一真实 user turn 内替换候选、合并 partial，或在最终回答缺失时
回退显示最后一条可用候选。

这些消息的共同点：

- 它们承载真实状态解释、真实中断结果、真实结束语义，或当前产品明确允许其进入结果历史

Agent 运行时 `messages`：

```json
[
  {"role":"user","content":"读取构建日志并总结失败原因。","_turn_key":"turn:30"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

Agent durable transcript：

```json
[
  {"role":"user","content":"读取构建日志并总结失败原因。","_turn_key":"turn:30"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

WebUI 展示投影：

```json
[
  {"role":"user","content":"读取构建日志并总结失败原因。","_turn_key":"turn:30"},
  {"role":"assistant","content":"","tool_calls":[{"id":"call_build","function":{"name":"read_build_log","arguments":"{}"}}]},
  {"role":"tool","tool_call_id":"call_build","content":"发现 3 条编译错误。"},
  {"role":"assistant","content":"Operation interrupted."}
]
```

**SSE 实时与 `done`**

partial assistant 是先通过 token 显示、再写回 durable；本地错误、guardrail 和 finalizer
闭合则可能没有 token，只在 `done` 的最终 session 中出现。这两种都不是内部 user 气泡。

取消的工具、未知工具名和历史损坏修复产生的 `tool` result 也不是 scaffold。
若它们进入展示层，应保持工具卡片语义，尤其不能把“已取消”显示成执行成功。

## 9. 一张速查表

本表同时列出目标 durable 语义和当前实现状态。目标遵循“Agent 尽量保持上游、
WebUI 一问一答投影”的边界，不表示当前代码已经全部完成。

- ✅ **已实现**：当前统一语义投影已覆盖，且有对应回归测试。
- ⚠️ **部分实现**：终态/历史语义正确，或消息能保留，但实时展示或专用渲染未完成验证。
- — **无需接入**：机制只在 Agent 内部或 provider 请求副本中运行，不进入主 WebUI transcript。

| 类别 | 补 `user` | 补 `assistant` / `tool` | SSE 实时会发生什么 | 目标 durable / WebUI | 当前状态 |
|---|---|---|---|---|---|
| verify-on-stop | 是 | 真实候选 | 候选更新当前回答区域；nudge 不发 user SSE | durable 保留候选和最终回答；WebUI 最终优先、候选兜底 | ✅ Agent durable 与 WebUI 收口已实现 |
| pre_verify hook | 是 | 真实候选 | 与 verify-on-stop 相同 | durable 保留候选和最终回答；WebUI 最终优先、候选兜底 | ✅ Agent durable 与 WebUI 收口已实现 |
| empty response recovery | 是 | 是 | 工具可见；内部 `(empty) + user` pair 不发角色事件 | pair 不保存、不显示 | ✅ 统一 `internal_scaffold` 过滤 |
| empty terminal sentinel | 否 | 是 | 不发 token/interim；由终态失败处理表达 | sentinel 不显示 | ✅ legacy flag 与统一语义过滤 |
| thinking prefill | 否 | 是 | 可能只有 reasoning；预填充不发 interim | 只留后来真实正文 | ✅ legacy flag 与统一语义过滤 |
| dropped tool-call recovery | 是 | 是 | 上游重试时不应生成角色气泡 | 上游 pair 不保存、不显示 | ⚠️ 当前 Fork 尚未实现或分类该上游分支 |
| length continuation | 是 | 已输出的 partial | partial 和续写更新同一回答区域；anchor 不发 user SSE | durable 保留 partial、anchor、续写；WebUI 合并为一条 | ✅ anchor 持久化与顺序合并已实现 |
| Codex ack continue | 是 | 中间 ack | ack 更新当前回答区域；anchor 不发 user SSE | durable 保留 ack、anchor、工具和最终回答；WebUI 最终优先 | ✅ ack/anchor durable 与收口已实现 |
| kanban stop guard | 是 | 是 | 候选 token 可能先出现；状态/工具另行发事件 | pair 被过滤，保留看板终态和最终回答 | ⚠️ 终态/历史过滤已实现；已流出的候选可能保留 |
| Codex incomplete continuation | 必要时 | incomplete assistant | incomplete 更新当前回答区域；anchor 不发 user SSE | durable 保留 incomplete、anchor、最终回答；WebUI 最终优先 | ✅ anchor durable、最终替换与候选兜底已实现 |
| max-iterations summary | 是 | summary assistant | anchor 无 user SSE；summary 按 provider 流式或等 done | durable 保留 anchor 和 summary；WebUI 只显示 summary | ✅ anchor durable 与 WebUI 隐藏投影已实现 |
| interrupt partial / tool-tail closure | 否 | 是 | partial 是已发 token；工具尾闭合通常由 done 出现 | 真实 transcript 收尾 | ✅ 按真实 transcript 行保留 |
| interrupt skipped tool | 否 | 仅 tool | 不发 user SSE；工具卡片按 transport 事件更新 | 保留“已取消、未执行”的 tool result | ⚠️ 不会被控制消息过滤；取消专用卡片未验证 |
| compression todo / anchor | 是 | 否 | 不产生 user SSE，不是实时聊天输入 | Fork 独立存 anchor；上游可合并到真实 user | ✅ Fork 语义已实现；与上游 durable 形状不同 |
| compaction summary anchor | 是 | 否 | 不产生 user SSE，不是实时聊天输入 | Fork 独立存 anchor；上游在冲突时可合并到 tail | ✅ Fork 语义已实现；与上游 durable 形状不同 |
| invalid tool JSON | 否 | assistant + tool | 失败/重试通过 tool 事件呈现 | 保留可恢复工具轨迹 | ⚠️ 真实行会保留；专用失败卡片未验证 |
| unknown tool name | 否 | assistant + tool | 失败/重试通过 tool 事件呈现 | 保留工具名错误与跳过结果 | ⚠️ 真实行会保留；专用失败卡片未验证 |
| corrupted historical tool arguments | 否 | 仅 tool | 不发 user SSE；按 tool 语义呈现 marker | 修复参数并保留对应 tool 错误轨迹 | ⚠️ 真实行会保留；marker 专用展示未验证 |
| terminal background process wakeup | 空闲唤醒时是 | tool + assistant | completion_queue 后由 `process_wakeup` 新 stream；普通 wakeup 可能发 user 气泡 | 真实进程轨迹和结果可恢复；普通 wakeup 当前未统一隐藏 | ⚠️ 机制已实现；Fork anchor/原 turn 对齐待补 |
| repair_message_sequence | 否 | 原地合并/删除 assistant/tool | 不产生额外 user SSE | 修复后的 canonical 序列参与 durable flush | ✅ Agent 已实现；WebUI 无专用修复事件 |
| manual partial-compress seam | 可能插入 bridge user/assistant | 可能删除/改写 tool 轨迹 | `/compress here` 不是普通 chat stream | 压缩后的 canonical transcript 写入新会话或原地集合 | ⚠️ WebUI 仅消费结果，未专门标注 seam 清理 |
| background memory/skill review | 辅助 Agent 内部 | 辅助 assistant/tool | 不进入主 chat SSE | review fork `_persist_disabled`；主会话不写入 | ✅ 主会话隔离；无需主投影 |
| MoA guidance | 聚合器内部 | 否 | 不属于主 chat SSE | 不进入主会话 transcript | — Agent 聚合器内部，不需要 WebUI 接入 |

## 10. 对 WebUI / 展示层的直接启示

WebUI 应按真实 user turn 建立唯一回答区域，再按下面的顺序投影：

1. **隐藏 `internal_scaffold` 和 `context_anchor`，但不要回写 canonical transcript**
2. **将 verification / incomplete 候选写入当前回答区域，不追加永久气泡**
3. **若同一 turn 出现后续最终 assistant，用最终回答替换候选**
4. **若没有最终 assistant，回退显示最后一条非空候选**
5. **按顺序合并 length continuation 的 partial 和续写片段**

尤其要注意以下差异：

- verification continuation：assistant 候选和后续最终答案都持久化；只有 nudge 是
  上游 ephemeral scaffold。WebUI 最终只保留一个回答区域
- length、Codex incomplete、ack continue、max-iterations 的控制 user 应作为
  durable context anchor，而不是 internal scaffold
- Codex ack assistant 保持上游 durable 语义；WebUI 有最终回答时不单独显示 ack
- empty recovery：assistant 和 user 两边都是 scaffold
- kanban stop guard：assistant 和 user 两边都是 scaffold
- max-iterations summary：内部 user request 不显示，而最终 summary 是否逐字流出取决于所走的
  provider 分支，不能假定所有模型都在这一步继续 token streaming
- 工具因 stop 被跳过、名称不存在或历史参数损坏时，补写的是可恢复的 `tool` 轨迹；
  不能按内部控制消息过滤，也不能将取消结果渲染成成功
- `terminal(background=true)` 的普通 `process_wakeup` 目前不是 `context_anchor`；不能把
  `source` 或 `[IMPORTANT: ...]` 文案当作隐藏判据。若要避免第二个 user 气泡，应补结构化
  anchor 和明确的 origin turn 绑定
- `repair_message_sequence`、压缩 seam bridge 和 tool-pair sanitizer 是 Agent 的状态修复，
  WebUI 不应自行重算或再次删除消息

不得只按 `finish_reason="stop"` 判断可见回答：预算耗尽时，候选可能就是唯一交付。
也不得按 `[System: ...]` 文案匹配、修改 `repair_message_sequence`，或合并
Agent canonical 消息来实现展示层的一问一答。

## 11. 结论

Hermes Agent 的消息补充逻辑，本质上是在维护三个目标：

1. **继续驱动模型完成未完成的工作**
2. **修复或维持合法的消息序列**
3. **让 durable transcript 可恢复，同时让 WebUI 保持一问一答**

因此，`messages` 并不只是“用户输入 + 模型输出”的朴素流水，而是包含了一层 Agent 运行时控制平面。

要正确消费这些消息，无论是在 WebUI、Gateway、导出器，还是后续 resume /
compression / replay 逻辑中，都需要把以下四类区分清楚：

- 真实 human user turn
- 真实 assistant deliverable
- durable context anchor
- ephemeral runtime scaffold

只有把持久化与展示分开，才能既不泄漏内部控制消息，也不错误丢失真实答案，
同时把 Agent 核心改动压缩到少量生产位置的元数据接缝。
