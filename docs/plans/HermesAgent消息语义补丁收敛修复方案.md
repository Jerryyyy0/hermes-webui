# Hermes Agent 消息语义补丁收敛修复方案

- **状态：** Implemented（本地 worktree，待提交）
- **审查基线：** `8393369314`
- **当前分支：** `fix-artifact`
- **已提交补丁：** `93d827f139`、`1c640edca1`
- **关联方案：** [压缩摘要上下文锚点修复方案](./压缩摘要上下文锚点修复方案.md)
- **适用仓库：** `/Users/wzq/Downloads/NLP-PyProject/hermes-agent`、当前 Hermes WebUI fork

## 1. 背景与结论

当前补丁已经实现了主要业务目标：真实的连续 user 记录不再由
`repair_message_sequence()` 合并后写回 canonical transcript；压缩摘要可以作为独立的
`context_anchor/compaction_summary` 持久化；发送给 provider 时只在临时副本中合并连续
user，从而避免生成新的聚合 user 持久化行。

但当前分支还不能直接提交或作为长期补丁使用，原因不是核心目标未实现，而是补丁中混入了
四类额外风险：

1. 消息语义日志会在 INFO 级别记录正文，非字符串内容甚至可能完整写入日志。
2. verification/pre-verify 的真实 assistant candidate 被错误标成 `internal_scaffold`，改变了
   上游既有的展示和持久化行为。
3. provider-only user 合并逻辑被重新内联到上游核心大文件，增加了同步上游时的冲突面。
4. length continuation、Codex ack/incomplete 和 max-iterations 的上游 durable 控制消息
   被改成 `internal_scaffold`，以删除 Agent 上下文的方式实现 WebUI 隐藏。

本方案的目的不是扩展消息系统，而是把现有补丁收敛为少量、可验证、可回滚的语义接缝。

## 2. 修复目标

### 2.1 必须满足

1. 两条真实 user 记录不得在 canonical transcript 或 `state.db` 中合成一条新 user 记录。
2. `context_anchor/compaction_summary` 必须独立持久化，重启后仍能参与模型上下文恢复。
3. 压缩摘要不得成为 WebUI 可见 user 气泡、真实 turn 或 artifact/manifest 归属锚点。
4. 真实 tail 的 `content` 和 `api_content` 不因摘要分类或 provider 整形而改变。
5. provider-bound 消息必须满足对应 provider 的角色和内容格式要求。
6. provider-only 整形结果不得写回 Agent 内存 canonical history、`state.db`、WebUI session、
   sidecar 或 SSE user 事件。
7. 消息语义审计不得记录用户正文、压缩摘要正文、多模态载荷或 base64 内容。
8. verification assistant candidate 保持上游语义：它是真实 assistant 内容；只有驱动继续执行的
   user nudge 才是 `internal_scaffold`。
9. 上游原本持久化的控制 user 只增加 `context_anchor` 元数据，正文、顺序和 durable 结果不变。
10. WebUI 每个真实 user turn 只形成一个最终 assistant 回答区域；候选可回退、partial 可合并。
11. 核心文件只保留必要 import、条件分支或单行调用，不在上游大文件内复制整段业务逻辑。

### 2.2 明确不包含

1. 不迁移或启发式重写已经存在的历史聚合消息。
2. 不按摘要文本前缀猜测消息类别。
3. 不新增 `_turn_key`、来源身份或 WebUI turn provenance 的 Agent `state.db` schema。
4. 不承诺 `_turn_key` 和来源身份跨 Agent 重启保留；只保证压缩过程不改写内存中的现有字段。
5. 不同步或合并当前 `upstream/main`；后续由维护者自行同步。
6. 不调整 `conversation_loop` 的全局 prompt-cache 注入顺序。
7. 不借本补丁重构压缩器、恢复流程、CLI、Gateway 或 TUI。

## 3. 目标消息模型

```text
canonical / state.db：
  assistant: 上一次真实回复
  user:      压缩摘要（class=context_anchor, kind=compaction_summary）
  user:      当前真实请求

provider 临时副本：
  assistant: 上一次真实回复
  user:      压缩摘要 + 当前真实请求

WebUI 展示：
  assistant: 上一次真实回复
  user:      当前真实请求
  assistant: 当前 user turn 的唯一最终回答区域
```

这里的连续 user 是 durable/canonical 层的受限例外，不是 provider payload 的合法性放宽。
必须在代码和测试中同时证明：durable 层没有丢失语义边界，provider 层没有连续同 role。

## 4. 修复设计

### 4.1 接缝一：canonical repair 不合并 user

保留 `repair_message_sequence()` 对 assistant/tool 结构损坏的既有处理，但移除其对连续 user 的
破坏性合并。该函数不得再通过修改第一条 user 的 `content` 来吸收后续 user，也不得因合并
删除 `api_content`。

该变化适用于所有连续 user，而不只适用于 `context_anchor + real user`。原因是 Agent 当前没有
足够的 durable provenance 来可靠判断两条 user 是否属于同一真实提交；在不确定时应保留原始
记录，不能猜测性合并用户数据。

需要同步澄清以下契约：

- `repair_message_sequence()` 负责 canonical 层的 assistant/tool 结构修复。
- provider 严格交替由 provider-bound 整形负责。
- `repair_alternation=True` 不再意味着 durable user 行会被压缩成一行。
- durable/canonical 层允许“独立真实 user”及“context anchor + user”形成连续 user；任何直接
  消费 canonical history 的新 provider 出口都属于违规旁路。

### 4.2 接缝二：provider-only 纯函数合并 user

恢复并保留 `agent/message_sanitization.py` 中的纯函数：

```python
merge_adjacent_user_messages_for_provider(messages)
```

约束如下：

- 输入必须是本次 API 调用的 disposable copy。
- 不修改输入 list 或其中的 message dict。
- 支持 `str + str`、`list + list`、`str + list`、`list + str`。
- `None` 按空内容处理；对 provider schema 不支持的未知内容类型明确失败，不使用 `str()` 猜测。
- 保持内容顺序：摘要在前，真实 user 及其 `api_content` 在后。
- 保持已有 content block 和 `cache_control` marker 的相对顺序。
- 不写入 `_turn_key`、来源字段或任何 durable 标记。

核心调用点保持极薄：

1. `drop_thinking_only_and_merge_users()` 过滤 thinking-only assistant 后调用该 helper。
2. MoA `_reference_messages()` 直接调用该 helper，不反向依赖整套 runtime thinking sanitizer。
3. 最大迭代总结继续复用 `drop_thinking_only_and_merge_users()`。

不得把这段合并实现再次内联到 `agent_runtime_helpers.py`、`conversation_loop.py` 或
`moa_loop.py`。

### 4.3 压缩摘要保持独立 context anchor

保留当前 `mark_context_anchor(message, kind)` helper，使其只增加权威语义字段，不修改原
`role`、`content` 或其它元数据。

`ContextCompressor` 继续执行以下规则：

1. 普通边界沿用压缩器已有 role 选择。
2. 原 `_merge_summary_into_tail` 双碰撞场景改为独立 user-role summary anchor。
3. 所有新摘要带完整 end marker。
4. 真实 tail 使用 `_fresh_compaction_message_copy()` 原样复制。
5. 不再对真实 tail 调用 `drop_stale_api_content()`。
6. 摘要自身不继承真实 tail 的 `_turn_key`、来源身份或 `api_content`。

`_compressed_summary=True` 仅作为压缩器内部兼容标记；持久化和 WebUI 投影只能依赖
`_hermes_message_class` 与 `_hermes_scaffold_kind`。

### 4.4 恢复 verification candidate 的上游语义

撤销以下超范围行为：

- 不给 `finish_reason=verification_required` 的 assistant candidate 添加
  `internal_scaffold` 或 `_verification_stop_synthetic`。
- 不给 `finish_reason=verify_hook_continue` 的 assistant candidate 添加
  `internal_scaffold` 或 `_pre_verify_synthetic`。
- 不把既有“candidate 应展示/持久化”测试反向修改为“不展示”。

保留以下分类：

- verification/pre-verify 的 user nudge 是 `internal_scaffold`。
- empty-response、thinking-prefill、kanban stop 等原本已有 synthetic flag 的内部消息可以
  同时写入权威分类，但不得改变其既有展示、持久化或清理行为。

验收时必须覆盖 success、后续验证失败、预算耗尽复用、异常和取消五个出口。任何出口都不能
因为分类扩大而丢失真实 assistant candidate。

### 4.5 保留上游 durable 控制消息

以下控制 user 在上游基线中会持久化，目标只增加 `context_anchor` 元数据：

- `length_continuation`
- `codex_incomplete_nudge`
- `intent_ack_continuation`
- `max_iteration_summary_request`

Codex intermediate ack assistant 恢复为普通 durable assistant，不再标成
`internal_scaffold`。WebUI 有后续最终回答时不单独展示 ack；没有最终回答时可将最后一条
非空候选作为兜底。

这些改动只允许使用现有 `mark_context_anchor(message, kind)` 薄 helper。不得新增文本
启发式、修改消息正文、扩大 finalizer 清理范围，或让 `repair_message_sequence()` 承担
展示投影职责。

### 4.6 WebUI 一问一答投影

一问一答由 `integration/agent_message_semantics/` 实现，不写回 Agent canonical history：

1. `internal_scaffold` 与 `context_anchor` 不形成用户或助手气泡。
2. verification / incomplete 候选更新当前回答区域；后续最终回答替换候选。
3. 没有后续最终回答时，最后一条非空候选作为本轮交付。
4. length continuation 的 partial 与续写按原顺序合并。
5. 工具卡片独立展示，不计作第二个 assistant 回答。

不得只按 `finish_reason="stop"` 判断最终回答；预算耗尽复用的候选可能使用
`verification_required`、`verify_hook_continue` 或 `incomplete`。

### 4.7 移除正文日志与环境变量

删除 `HERMES_MESSAGE_SEMANTICS_LOG_MAX_CHARS` 和正文 preview 逻辑。

如仍需要诊断日志，只允许记录：

```text
action, class, kind, role, session_id, turn_key 是否存在
```

日志级别使用 DEBUG；不得记录 `content`、`api_content`、附件、多模态 block、工具结果、摘要
正文或真实 `_turn_key` 值。若确需关联同一内容，只能使用进程内不可逆短哈希，并默认关闭。

### 4.8 拆出与本目标无关的 context anchor

MCP reload、model switch、personality pivot 等 context anchor 分类不应与压缩摘要修复形成
不可分割提交。处理方式二选一：

1. 已有独立问题和测试证据时，保留为单独提交。
2. 没有独立证据时，先恢复基线行为，只保留方案要求的 `compaction_summary` 和已有
   `todo_snapshot` 兼容。

不能为了减少提交数而把多种用户可见行为塞进同一个消息语义提交。

## 5. 文件级变更范围

### 5.1 必要生产文件

| 文件 | 允许的改动 |
| --- | --- |
| `agent/agent_runtime_helpers.py` | 删除 canonical user 合并；调用 provider helper；不内联 helper 实现 |
| `agent/message_sanitization.py` | 放置无副作用的 provider-only user 合并 helper |
| `agent/context_compressor.py` | 独立生成 `compaction_summary`，删除新摘要 merge-into-tail 生产分支 |
| `agent/message_semantics.py` | 提供分类 helper；删除正文日志和新增环境变量 |
| `hermes_state.py` | nullable 语义字段写入、读取和旧库声明式列协调 |
| `agent/moa_loop.py` | 一行调用 provider helper |
| `agent/conversation_loop.py` | 恢复真实 assistant；仅在四类控制 user 生产点增加 context-anchor 薄标记 |
| `agent/chat_completion_helpers.py` | 将 summary request 从 internal scaffold 改为 context anchor |
| `run_agent.py` | 只过滤真正 ephemeral scaffold；context anchor 必须继续持久化 |

### 5.2 WebUI integration 文件

| 文件 | 允许的改动 |
| --- | --- |
| `integration/agent_message_semantics/projection.py` | 隐藏 anchor、候选兜底、最终替换和 partial 合并 |
| `integration/tests/agent_message_semantics/test_projection.py` | 一问一答投影行为测试 |
| `integration/tests/agent_message_semantics/test_state_db_replay.py` | context anchor 持久化但不可见的 replay 测试 |

### 5.3 其它接入文件

CLI、Gateway、TUI、ACP 只在确实需要传递语义字段时保留薄调用。纯注释变更不单独进入补丁；
若注释因行为变化已经错误，只在同一功能 hunk 中最小修正。

当前 worktree 中以下未提交修改主要是注释语义调整，不影响运行时，可从最终补丁移除以降低
冲突噪音：

- `acp_adapter/session.py`
- `agent/conversation_compression.py`
- `gateway/session.py`
- `hermes_cli/cli_commands_mixin.py`
- `hermes_state.py` 中仅注释的 worktree hunk
- `tests/agent/test_compressed_summary_metadata.py` 中仅说明文字的 hunk

## 6. 实施顺序

### 阶段 A：先消除已确认回归

1. 恢复 verification assistant candidate 的展示和持久化语义。
2. 恢复 Codex intermediate ack assistant 的上游 durable 语义。
3. 将四类 durable 控制 user 从 internal scaffold 改为 context anchor。
4. 恢复被反向修改的上游测试期望，并增加 anchor 持久化测试。
5. 删除 INFO 正文日志、非字符串完整日志和新增 `HERMES_*` 环境变量。

完成条件：即使后续消息合并补丁回滚，也不再存在新增的隐私和 assistant 丢失风险。

### 阶段 B：收敛 provider 接缝

1. 将当前 worktree 内联实现恢复到 `message_sanitization.py` 的纯 helper。
2. 主循环既有 `drop_thinking_only_and_merge_users()` 只保留一次 helper 调用。
3. MoA 直接调用纯 helper。
4. 枚举所有可能发送 canonical conversation 的出口，并证明它们经过 provider 整形：
   - 主 conversation loop；
   - 最大迭代总结；
   - MoA reference view；
   - retry 使用已经整形的同一请求副本；
   - Codex app-server 不发送 canonical transcript，记录为不适用。

完成条件：除纯 helper 外，不存在第二份连续 user 合并实现。

### 阶段 C：完成 WebUI 一问一答投影

1. 按真实 user turn 建立唯一回答区域。
2. 覆盖候选被最终回答替换、候选兜底和 length partial 合并。
3. 验证刷新、分页和 state.db replay 后仍保持一问一答。
4. 证明投影没有修改 canonical message、turn key 或 artifact/manifest 归属。

完成条件：同一真实 user turn 最多显示一个 assistant 回答，且任何可用最终内容都不丢失。

### 阶段 D：完成压缩摘要 anchor

1. 保留 role 的 `mark_context_anchor()`。
2. 删除新摘要拼接真实 tail 的生产分支。
3. 在双碰撞时生成独立 user-role summary anchor。
4. 验证 `state.db` 声明式列协调可从旧 schema 增加 nullable 语义列。
5. 验证压缩、重启恢复、provider wire、WebUI projection 的完整链路。

完成条件：摘要在 durable 层独立存在、WebUI 不展示、provider 仍可读取、真实 tail 不变。

### 阶段 E：整理提交边界

分成至少四个逻辑提交，不重写已经推送的历史；先追加纠正提交，PR 合并前再由维护者决定是否
squash：

1. `修复：恢复上游消息持久化语义并移除正文日志`
2. `修复：仅在 provider 副本合并连续用户消息`
3. `修复：将压缩摘要持久化为独立上下文锚点`
4. `修复：在 WebUI 将单轮消息收口为一个回答`

MCP/model/personality 等其它类别如需保留，使用第四个独立提交。

## 7. 测试矩阵

### 7.1 Canonical 与 provider

| 场景 | canonical/state.db | provider payload |
| --- | --- | --- |
| 两条真实 user | 保留两行 | 合并为一条临时 user |
| summary anchor + 真实 user | 保留两行及各自元数据 | 摘要在前、真实请求在后 |
| 三条连续 user | 保留三行 | 按原顺序合并为一条 |
| multimodal + text | 原始 block 不变 | block 顺序不变 |
| 未知 content 类型 | 不修改 canonical | 明确失败，不静默发送非法序列 |
| thinking-only assistant 被移除后形成 user/user | canonical 不变 | 临时合并 |

### 7.2 压缩与恢复

必须覆盖：

- head=assistant、tail=user 双碰撞；
- head=user、tail=assistant 双碰撞；
- user-role 和 assistant-role 独立摘要；
- 真实 tail 带 `api_content`、`_turn_key` 和来源字段；
- 压缩后写入 `state.db`、关闭、重新打开并恢复；
- 恢复后主 provider 和 MoA 请求；
- prompt cache 开启时 content block 和 marker 顺序；
- 旧 `state.db` 缺少新列时的启动协调。

对 `_turn_key` 和来源字段的断言分两层：

- 压缩函数内存结果必须原样保留。
- 重启后不作保留断言，并在测试名称中明确“当前 schema 不持久化 provenance”。

### 7.3 Verification

必须恢复并扩展以下行为测试：

- candidate 被 interim callback 正确处理；
- candidate 在 user nudge 被删除后仍写入 durable transcript；
- 后续真实 final response 可以替代 candidate；
- 预算耗尽复用 candidate 时只形成一个最终 assistant 行；
- 验证异常、取消或进程重启后 candidate 仍可恢复；
- user nudge 永远不进入 durable transcript。

### 7.4 Durable 控制消息与一问一答

必须覆盖：

- 四类控制 user 均以 context anchor 写入 `state.db`，WebUI replay 不显示；
- Codex ack assistant 持久化，有最终回答时不形成第二个气泡；
- verification / incomplete 没有后续最终回答时可回退显示候选；
- length partial 与续写按顺序合并，内容不重复、不丢失；
- 不能仅凭 `finish_reason="stop"` 决定是否显示；
- 分页、刷新、恢复和实时 SSE 收口得到相同的一问一答结果。

### 7.5 日志安全

通过 `caplog` 或隔离日志文件断言以下敏感值不存在：

- 普通用户正文；
- compaction summary 正文；
- `api_content`；
- 附件路径和 workspace 补充文本；
- 多模态 data URL/base64；
- 真实 `_turn_key` 值。

## 8. 验证命令

Agent 侧使用仓库测试入口：

```bash
scripts/run_tests.sh \
  tests/agent/test_message_semantics.py \
  tests/agent/test_context_compressor.py \
  tests/agent/test_api_content_sidecar.py \
  tests/agent/test_compaction_summary_anchor.py \
  tests/run_agent/test_message_sequence_repair.py \
  tests/run_agent/test_thinking_only_sanitizer.py \
  tests/run_agent/test_moa_loop_mode.py \
  tests/run_agent/test_verification_continuation_budget.py \
  tests/hermes_state/test_restore_alternation_repair.py \
  tests/gateway/test_retry_replacement.py -q
```

然后执行：

```bash
ruff check <本次修改的 Agent Python 文件>
git diff --check 8393369314
```

WebUI 侧通过 `./scripts/test.sh` 运行消息语义 replay、分页、turn 和 manifest 测试。最终还需在
隔离的 `HERMES_HOME` 与 `HERMES_WEBUI_STATE_DIR` 中执行一次 Agent 压缩、重启、WebUI 加载的
端到端验证，禁止使用真实用户状态目录。

至少运行：

```bash
./scripts/test.sh \
  integration/tests/agent_message_semantics/test_projection.py \
  integration/tests/agent_message_semantics/test_state_db_replay.py \
  integration/tests/agent_message_semantics/test_turn_contract.py -q
```

## 9. 验收门槛

只有同时满足以下条件才可标记为已完成：

1. 原始两条真实 user 在 Agent `state.db` 中仍是两行。
2. WebUI 不再产生或展示由两条真实请求拼成的新 user 气泡。
3. provider payload 不含连续 user，且顺序和 sidecar 内容正确。
4. compaction summary 在重启后仍存在，但不成为 WebUI turn。
5. verification candidate 可持久化、可实时更新当前回答区域；有最终回答时不形成第二个
   永久气泡，无最终回答时可兜底；user nudge 不可持久化。
6. 四类控制 user 以 context anchor 持久化，但不成为 WebUI user 气泡。
7. 同一真实 user turn 最多显示一个 assistant 回答；候选兜底和 partial 合并不丢内容。
8. 日志中不存在消息正文和多模态载荷。
9. 没有新的非秘密 `HERMES_*` 环境变量。
10. provider helper 只有一份实现，核心调用点是薄接缝。
11. 相关测试在基线失败、修复后通过，并记录失败原因。
12. 明确记录未验证项：真实 provider 缓存命中率、未来上游兼容性、provenance 跨重启。

## 10. 风险与回滚

### 10.1 主要残余风险

- 新 provider 出口绕过 provider helper，直接发送 canonical 相邻 user。
- 上游后续恢复全局 user 合并，重新产生聚合持久化行。
- prompt-cache marker 虽保持顺序，但不同 provider 的真实缓存命中尚未实测。
- 旧历史聚合消息继续存在；本补丁只阻止新问题产生。
- WebUI 错误识别 turn 边界时，可能把相邻真实 assistant 错误折叠。
- 候选兜底遗漏非 `stop` finish reason 时，预算耗尽场景可能没有可见回答。

### 10.2 回滚方式

1. 优先按逻辑提交逐个 revert，不重置分支或删除用户工作树。
2. 回滚 compaction summary 写入时保留 nullable 数据库列；旧列无行为影响，无需破坏性迁移。
3. 发布顺序保持 WebUI 先具备通用 `context_anchor` 过滤能力，再启用 Agent 新 kind。
4. 如果 provider 合并出现故障，回滚 provider 接缝提交，不得通过重新启用 canonical 持久化
   合并来规避错误。

## 11. 最终建议

当前分支应先恢复上游 durable 语义，并把四类控制 user 收窄为持久化的 context anchor；
随后在 WebUI integration 完成一问一答投影。provider helper 和压缩摘要继续按独立提交
收敛。不要同步上游，也不要让 Agent 的 finalizer、持久化或序列修复承担展示职责。
