# 会话 User 聚合消息修复方案

## 状态与范围

本文是会话 transcript 一致性问题的修复方案，不执行数据迁移，也不
修改任何现有会话。问题发生在 Agent 持久化的消息、WebUI 侧车和展示
投影三层的边界：两个不同的 user 请求可能被历史压缩/重建过程拼接为
一条派生 user 行；若该行又复用已有 `id` 或 `_turn_key`，WebUI 会错误
地隐藏真实请求或把 assistant 活动归属到错误的 user turn。

本方案涉及两个仓库：

| 所有者 | 范围 |
| --- | --- |
| Hermes Agent | `state.db` 消息写入、压缩/上下文重建时的消息语义 |
| Hermes WebUI | 侧车与 `state.db` 的合并、展示投影、turn/manifest 诊断 |

WebUI 不应直接修补真实 `state.db` 的历史行。Agent 是消息写入事实的
所有者；WebUI 只维护自己的展示侧车和可逆的修复元数据。

## 问题模型

一次异常记录在真实用户交互中会是这样：

```text
用户先发送：
  帮我设置定时任务，每三分钟获取今日杭州天气，并生成一个 Markdown 文件。

此时 assistant 尚未给出回复，用户又发送：
  请根据下面的大纲补充内容，先写 PPT 讲稿，再做一个深蓝色国企风格的 HTML 演示稿。
  [附带一个集团简介文档]
```

正常 transcript 应保留两条独立的 user 消息，各自对应不同的 turn：

```text
用户：帮我设置定时任务，每三分钟获取今日杭州天气，并生成一个 Markdown 文件。
assistant：<对天气定时任务的回复>

用户：请根据下面的大纲补充内容，先写 PPT 讲稿，再做一个 HTML 演示稿。
assistant：<对 PPT/HTML 的回复>
```

本次异常中，持久化历史额外出现了第三条并非用户实际发送的 user 消息：

```text
用户（错误生成）：
  帮我设置定时任务，每三分钟获取今日杭州天气，并生成一个 Markdown 文件。

  [Workspace::v1: <工作区>]
  请根据下面的大纲补充内容，先写 PPT 讲稿，再做一个 HTML 演示稿。
  [Attached files: <集团简介文档>]
```

这条“错误生成”的消息把两次真实请求、WebUI 的工作区前缀和附件提示
拼成了一条新的 `role=user` 行。它不是新的用户意图，却可能复用第二条
请求的 `id`，或与第二条请求共享 `_turn_key`。目前的投影逻辑仅按
`_turn_key` 将 user 视为 replay，因而可能隐藏内容不同的真实 user 行。

旧式清理器也无法可靠清除它：清理器按 `"\n\n"` 拆分消息，但上面的
两条真实请求本身都可能有空行、交付说明、附件和 workspace 前缀。拆分后
得到的是多个片段，而不是原来的两条完整 user 消息，因此无法安全匹配。

这违反了以下既有约束：

- 可见 transcript 必须呈现一条按时间顺序的用户故事，不能隐藏启动
  工作的 user turn。
- `_turn_key` 是真实 user turn 的稳定锚点；冲突时不能猜测或重用归属。
- 压缩摘要是 Agent 的恢复材料，不能成为新的用户意图。

参考：`docs/rfcs/webui-run-state-consistency-contract.md`、
`docs/architecture/turn-key-backend.md`。

## 目标与不变量

修复完成后，必须满足：

1. 每个真实 user 提交保留独立、不可变的来源身份和 `_turn_key`。
2. 内容不同的 user 行即使共享旧 `id`、时间戳或 `_turn_key`，也不得
   被去重、隐藏或合并。
3. 同一真实消息在侧车、`state.db`、SSE/replay 中出现多份时，只显示
   一次，且只能在来源身份和语义内容都一致时折叠。
4. 压缩或上下文重建产生的材料不能以普通 `role=user` 出现在展示
   transcript 中。
5. 无法可靠判定的历史记录必须保留并产生诊断，不能因内容启发式而
   自动删除或重写。
6. assistant/tool 活动只有在 user 锚点唯一时才能归属到 turn；key 冲突
   时写入 diagnostics/orphan，而不是绑定到相邻 user。

## 实施方案

### 1. 先落 WebUI 防丢保护

该步骤不等待 Agent 改动，可立即阻止真实 user 消息在加载时消失。

1. 修改 `integration/agent_message_semantics/projection.py`：
   `_dedupe_replayed_users()` 只能在以下条件同时满足时去重：
   `_turn_key` 相同、规范化后的可见 user 内容相同，且来源身份兼容。
   不能只因 `_turn_key` 相同就删除后来的 user 行。
2. 对相同 `_turn_key` 但不同内容的 user 行，保留两行并记录结构化
   `turn_key_conflict` 诊断。不要把 assistant/tool 自动归属给其中任一行。
3. 在 `api/models.py` 的侧车/`state.db` 合并路径中，停止把裸 `id` 当作
   跨来源的唯一身份。为 `state.db` 行使用带来源域的身份，例如
   `state_db:<profile>:<rowid>`；侧车行只有在已保存同一来源身份时才能
   使用该身份匹配。
4. 保留现有内容和时间戳比较作为辅助条件，不允许它们覆盖来源冲突。
   同文本的不同真实 turn 仍必须保留。

防丢保护的预期结果是：异常历史可能暂时显示两条 user 行，但不会再
静默丢掉后一条真实请求。这是比错误关联更安全的降级行为。

### 2. 修正 Agent 写入与压缩语义

在 Hermes Agent 中追踪所有会把相邻 user 消息拼成单个列表元素的
路径，尤其是 `ContextCompressor`、上下文重建和 `state.db` 持久化边界。

1. 禁止把多个真实 user 请求拼接成新的普通 `role=user` 消息。
2. 若模型上下文确实需要聚合文本，聚合物只能存在于 context-only
   数据中，并显式标记为内部 scaffold/compaction material；不得写入
   transcript、不得继承真实 user 的来源身份或 `_turn_key`。
3. 新写入的真实消息必须携带不可变的 `source_message_id`，并将该值
   从 Agent `state.db` 到 WebUI 读取器完整透传。SQLite `messages.id`
   可作为该数据库内的来源主键，但跨来源比较必须带上 profile/database
   域，不能裸用数值。
4. 每次创建真实 user 行时分配一次 `_turn_key`；压缩、retry、replay 和
   sidecar 同步只能复制该 key，不能为不同内容复用它。
5. 在 Agent 写入前加入校验：同一 session 中，若已有相同
   `source_message_id` 或 `_turn_key` 的 user 行而内容指纹不同，拒绝
   写入该派生 user 行并记录无内容日志的冲突诊断。

此步骤需要在 Agent 仓库单独提交并验证；WebUI PR 只包含读取协议和
兼容处理，避免将 Agent 业务逻辑散落到 WebUI 核心模块。

### 3. 收紧 WebUI 合并与 turn 归属

1. 为合并键、去重键和展示键分别定义用途：
   来源身份用于“是否同一消息”，内容用于“是否同一可见文本”，
   `_turn_key` 仅用于“已验证的 turn 归属”。三者不得互相替代。
2. 更新 `merge_session_messages_append_only()`：来源身份冲突或内容冲突
   时保留两条消息，不通过 `id` 元数据合并。
3. 更新 manifest/replay 结算：发现一个 `_turn_key` 对应多个不同 user
   内容时，进入 `diagnostics.conflicting_turn_keys`；相关 artifact 仅保留
   为顶层 orphan，不能进入任一普通 `turns[]`。
4. 保留并扩展 `pending_turn_key`/`stream_turn_key` 校验，确保当前 worker
   只可结算到与其提交 user 完全一致的来源身份和 turn key。
5. 删除以 `"\n\n"` 切分普通 user 内容来判断聚合行的自动修复策略。
   新格式依赖显式 provenance；旧格式不能仅凭文本外观被隐藏。

## 历史会话处理

历史数据的优先级是“不丢失”，不是“立刻变得整洁”。

1. 新增只读审计工具，输出 session 级计数和匿名指纹：
   - 相同来源身份但不同内容；
   - 相同 `_turn_key` 但不同内容；
   - 相邻 user 之间没有 assistant；
   - 可证明的 Agent 内部 scaffold 出现在可见 transcript。
2. 审计默认使用隔离的 `HERMES_HOME`/`HERMES_WEBUI_STATE_DIR` 运行，
   对真实状态只允许显式指定 session 的只读模式；日志不得打印完整
   prompt、附件路径、Cookie 或密钥。
3. 仅为带有新的显式 provenance 的行做自动展示修复。旧会话生成候选
   清单，不自动删除、合并或更新 `state.db`。
4. 对已确认的旧聚合行，使用 WebUI 自己的追加式修复映射保存“聚合行
   -> 规范 source_message_id 列表”。映射必须记录规则版本、证据和
   创建时间，并允许撤销；原始侧车和 Agent 数据库保持不变。
5. 映射无法证明时，UI 保留原始行并显示冲突诊断，供管理员导出或
   人工处理。不能用相邻文本、空行、相似度或相邻 turn 编号猜测。

## 测试计划

每个测试先在未修复代码上复现失败，再验证修复后通过。

| 场景 | 断言 |
| --- | --- |
| 两个包含空行的连续 user 请求 | 两行保持独立，不生成第三个普通 user 聚合行 |
| 相同 `_turn_key`、不同 user 内容 | 两行均可见，产生冲突诊断，不发生投影删除 |
| 相同内容、不同真实 turn | 两个 turn 均保留，不被内容去重 |
| 同一 `source_message_id` 的 sidecar/`state.db` replay | 只显示一次，并保留展示元数据 |
| 两个来源域复用相同数值 `id` | 不视为同一消息 |
| workspace 前缀、附件段和多段交付模板 | 不触发基于空行的聚合清理 |
| 自动压缩、手动压缩、retry、cancel、重连 | user 顺序、`stream_turn_key` 和 assistant 归属保持一致 |
| `msg_limit`、`msg_before`、`turn_align` 分页 | 任一分页窗口不制造或隐藏 user turn |
| manifest/artifact 结算遇到 key 冲突 | 不写错误的 per-turn record，保留 orphan diagnostics |

建议新增：WebUI 合并/投影单测、`/api/session` 集成测试、SSE/replay
回归测试，以及 Agent 的 context/persistence 单测。对已捕获的异常会话
只使用脱敏、最小化 fixture，不将真实 prompt 或本机路径提交到仓库。

## 分阶段交付与验收

当前已实施的第一阶段：Agent 将连续 user 的合并限定在 provider API 副本，
不再原地改写 canonical `messages`；WebUI 对相同 `_turn_key`、不同内容的
user 行保留并记录诊断，同时停止依据空行自动删除旧行。来源域、manifest
orphan 和历史审计工具仍是后续独立工作，不在本次修复中推断或改写历史数据。

1. **WebUI 防丢 PR**：投影不再按单独 `_turn_key` 删除 user，合并键加入
   来源域；通过上述冲突和分页测试。
2. **Agent 写入 PR**：停止产生普通 user 聚合行，持久化
   `source_message_id`，并对 key/content 冲突失败关闭。
3. **跨仓库兼容 PR**：WebUI 消费新 provenance，manifest 对冲突降级为
   diagnostics/orphan；在隔离状态下运行 Agent -> WebUI 的端到端压缩与
   重连测试。
4. **历史审计工具**：先 dry-run 统计候选，再仅对可证明记录写入可撤销
   映射。真实会话修复须经人工确认。
5. **上线观察**：监控匿名计数 `conflicting_turn_key`、
   `source_identity_conflict`、`legacy_aggregate_candidate`。连续观察期为
   零后才考虑收紧旧兼容分支。

验收标准是：真实 user 不消失、不被拼接成当前意图、assistant 不跨
user turn 归属；重复 replay 仍只显示一次；历史原始数据可完整回读。

## 风险与非目标

- 不在本次修复中重写 Agent `state.db` 的既有历史。
- 不根据文本相似度自动清理旧消息，避免把用户主动粘贴的内容误删。
- 不改变消息分页协议或引入新的前端框架/长期服务。
- 若 Agent 版本与 WebUI 版本独立升级，WebUI 必须把缺少
  `source_message_id` 的记录视为 legacy，并采用保留优先的行为。
