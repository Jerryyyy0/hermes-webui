# Cron 压缩恢复 `user_message` 重复修复方案

- **状态：** Proposed
- **范围：** 已结束 Cron execution 的 WebUI transcript 投影、Manifest turn 归属与回归测试；后续可选的 Hermes Agent 语义增强。
- **关联文档：** [压缩摘要上下文锚点修复方案](./压缩摘要上下文锚点修复方案.md)、[Hermes Agent 消息语义补丁收敛修复方案](./HermesAgent消息语义补丁收敛修复方案.md)、[Cron Execution Identity 与 Artifact 结算修复](../rfcs/cron-session-manifest-artifact-settlement.md)。
- **不包含：** 删除或迁移历史 `state.db` 行、重跑 Cron、修改现有任务 prompt、全局按文本去重，或改变 Agent 发送给模型的压缩上下文。

## 1. 当前现状

### 1.1 已确认的样本形态

本次问题发生在一个单次、长工具链的 Cron execution 中。该 execution 只有一个 `cron_*` session ID 和一段连续的运行时间，并非调度器将同一 job 触发为两个 execution。

脱敏后的状态库证据如下：

| 层 | 观察到的内容 | 结论 |
| --- | --- | --- |
| Agent `state.db.messages` | 初始 Cron prompt 写入一次；两次 context compaction 后又各写入一次相同 prompt；三条正文长度和内容完全一致。 | 重复首先产生在同一 Agent execution 的 canonical transcript 内。 |
| active `state.db` 投影 | compaction 会淘汰一部分旧行；最终活跃视图留下初始 prompt 与最后一次恢复的 prompt。 | 物化时可见两条相同真实 `role=user` 行。 |
| WebUI Cron sidecar | 含初始 prompt、`context_anchor/compaction_summary`、工具尾部、同一 prompt 的恢复行；两条 user 分别被标为 `turn:1`、`turn:2`。 | UI 的重复气泡和 `user_message_count` 异常来自持久化投影，不是前端重复渲染。 |
| Cron scheduler | 一个 job run 对应一个 execution/session；无第二个 delivery 或第二个 scheduler claim 的证据。 | 排除“同一 job 被调度两次”。 |

该样本中 WebUI sidecar 的归一化回放仍得到两条相同真实用户消息，并为其连续分配 `turn:1`、`turn:2`。因此现象可在不访问网络、不运行真实任务的情况下稳定复现。

### 1.2 当前定时任务会话的消息切分逻辑

当前实现不是把 Cron 消息当作普通聊天数组直接展示，而是经过两次不同目的的切分：Agent 的**压缩/活跃消息切分**，以及 WebUI 的**一次 execution 与后续人工 follow-up 切分**。两者的边界不同，必须区分。

#### A. Agent：canonical transcript 与 compaction active view

Cron scheduler 只向 Agent 提交一次 assembled prompt，并以本轮 `cron_*` session ID 写入 `state.db.messages`。随后 Agent 运行工具链；当上下文过长时，压缩器会：

1. 将可压缩的较早消息折叠为一条 durable `context_anchor/compaction_summary`；
2. 保留最近真实 user，以确保下一次模型调用仍知道正在执行的任务；
3. 保留与当前执行连续的 assistant/tool 尾部；
4. 将被压缩的旧消息从 active view 排除，但不把它们作为 WebUI 的业务 turn 删除。

因此，**“压缩后恢复的 user”在 Agent 侧不是一次新的 Cron 提交**，而是同一个初始 prompt 为模型上下文连续性留下的 active 副本。一次长任务可能压缩多次，也就可能生成多个同正文恢复副本。

```text
一次 scheduler 提交
  P (初始 Cron prompt)
  -> assistant/tool 链
  -> [compaction_summary] + P (恢复副本) + assistant/tool 尾部
  -> [compaction_summary] + P (再次恢复副本) + assistant/tool 尾部 + 最终答复
```

这里的 `P` 语义上仍是同一请求；Agent 需要它继续推理，但它不应在 WebUI 中变成多个用户发起的业务轮次。

#### B. WebUI：execution prefix 与 follow-up suffix

Cron execution 结束后，WebUI sidecar 可以被普通聊天继续使用。WebUI 以 durable 的 `cron_execution_ended_at` 作为唯一切分边界：

```text
messages[0 : split_at]  = execution prefix
messages[split_at : ]   = WebUI follow-up suffix
```

`cron_execution_prefix_and_suffix(session)` 从消息数组开头扫描；第一条 `timestamp > cron_execution_ended_at` 的消息开始即为 suffix。它还会验证 suffix 中所有消息都确实晚于该边界；任何缺失、非数值或乱序边界都返回 `None`，使 reply prepare fail closed，而不是猜测切分。

这条边界有两个重要后果：

- execution prefix 是 Agent 所有权范围：其中只应投影一次初始 Cron 请求，以及它的工具活动和最终交付；
- suffix 是 WebUI 所有权范围：其中的 user 消息是新的人工 follow-up，即使正文与 `P` 完全相同，也必须保留为新 turn。

本次样本的重复恢复行时间戳仍不晚于 `cron_execution_ended_at`，所以它属于 prefix；它不是 suffix 中的人工重试。

#### C. WebUI：物化、reconcile 与展示路径

当前代码的实际数据流如下：

```text
Agent state.db active messages
  -> materialize_cron_session()（首次创建 sidecar）
  -> reconcile_cron_session_transcript()（已有 sidecar 时）
       1. 切出 prefix / suffix
       2. 只读取 execution profile 的 state.db 消息
       3. 仅保留 timestamp <= cron_execution_ended_at 的 DB 行
       4. 与 sidecar prefix 做 append-only merge
       5. 保留原 suffix 不动
       6. normalize_cron_manifest_messages(prefix)
       7. _stamp_cron_manifest_turn_keys(prefix)
  -> sidecar.messages = normalized_prefix + untouched_suffix
```

`settle_materialized_cron_session()` 在用户开始 follow-up 前再次执行同一 prefix/suffix 切分：先 reconcile prefix，再归一化、写入 sidecar、结算 prefix 中的 Manifest turn，最后把未改动 suffix 拼回。这样 Artifact 只应属于原 Cron execution，而 follow-up 使用下一个 turn key。

`GET /api/session` 的 Cron 路径会触发 reconcile；`GET /api/session/manifest` 则为只读展示路径，它会合并 sidecar 与 `state.db`，在已有稳定 key 的条件下调用 Cron normalize。Manifest GET 本身不允许写 sidecar 或 Agent 数据。

#### D. 本次重复如何穿透切分

当前 normalize 先移除已知 max-iteration 内部请求，并可调整“anchor 后只剩一条恢复 user”的顺序；随后为每条 `_is_real_user_message()` 分配连续 `_turn_key`。它没有 execution-prefix 内的“compaction 恢复副本”语义，因此结果为：

```text
prefix（实际样本的可见投影）
  P                         -> turn:1
  context_anchor            -> 不占用 turn
  assistant/tool 尾部        -> 归属 turn:1
  P（compaction 恢复副本）   -> turn:2  ← 错误的业务切分
  最终 assistant             -> 归属 turn:2 / 产生错误的后续结算风险
```

问题不在 prefix/suffix 边界本身，而在 prefix 内缺少对“跨 compaction anchor 的同一初始 prompt 恢复副本”的投影折叠。第 3 节的修复只补这个缺口，绝不跨越 `cron_execution_ended_at` 修改 suffix。

### 1.3 当前责任边界

```text
Cron scheduler
  -> Agent canonical transcript (state.db)
      -> compaction summary + 被保护的最后一个真实 user
  -> WebUI materialization/reconciliation
      -> Cron sidecar (可见会话、turn key、Manifest 输入)
  -> WebUI/Manifest display projection
```

Agent 压缩器的 `_ensure_last_user_message_in_tail()` 有意保护最近真实用户消息。否则长工具链被压缩后，模型会把仍在执行的任务误当成历史摘要而丢失。该保护是正确的运行时行为，不能为了隐藏重复而从 Agent canonical transcript 中删除恢复行。

问题位于 WebUI：`normalize_cron_manifest_messages()` 当前能识别 context anchor、最大工具轮次总结请求，以及“压缩后只剩一个真实 user”时的顺序修复；但它明确保留多个真实 user。于是同一 Cron execution 内、跨 `compaction_summary` 恢复的相同 prompt 被误认为两个独立业务 turn。

### 1.4 已有修复为何未覆盖

已有 Cron 归一化修复解决的是：context anchor 后只有一条恢复 user 时，恢复 user 被落在 assistant/tool 尾部之后的顺序问题。它的保护条件要求 transcript 只有一个真实 user，以避免把多轮对话或真正 follow-up 当作重复。

本次样本在第二次压缩后同时含有“压缩前的初始 prompt”和“压缩后恢复的相同 prompt”，因此不满足单-user 条件。现有 `preserves_multiple_real_turns` 测试也只验证不同正文的多轮消息，不覆盖同一 execution prefix 内重复恢复原 prompt 的形态。

## 2. 目标与不变量

### 2.1 修复目标

1. 一个 Cron trigger 的 execution prefix 中，初始用户 prompt 经 compaction 恢复后不得再形成第二个可见 user bubble 或第二个 Manifest turn。
2. 修复后该 execution prefix 只保留一个对应 prompt 的真实 user turn，且其已有 `turn:1` 必须稳定。
3. 后续 WebUI follow-up 仍是新 turn；即使其正文恰好与初始 prompt 相同，也绝不能被移除。
4. Agent `state.db` 中的原始 compaction/recovery 行不修改；模型运行期间的上下文连续性不改变。
5. `context_anchor` 继续 durable、继续对模型可用、继续不作为可见 user/Manifest turn。
6. Manifest 的 Artifact decision 仍按保留下来的真实 turn 结算；不得把同一工具活动重复归属到 `turn:2`。
7. 物化、reconcile、Cron follow-up prepare 与 Manifest GET 对同一 sidecar 得到一致的可见 turn 集合。

### 2.2 非目标与禁止项

- 不在所有会话、所有 Cron 历史或任意相同 user 文本间做去重。
- 不按时间戳相近、内容前缀相同或字符串模糊匹配推断重复。
- 不删除 Agent `state.db` 历史，也不在 `GET /api/session/manifest` 中写库或修复历史。
- 不把 compaction 的恢复行改成 `context_anchor`；它在 Agent 侧仍是维持执行上下文的真实 user 消息。
- 不修改 `cron/scheduler.py` 的 job claim、delivery、session allocation 或 workspace binding。

## 3. 推荐修复：仅规范化已结束 execution prefix

### 3.1 识别规则

在 WebUI 已能证明一段消息属于**同一个已结束 Cron execution prefix**时，新增一个纯函数，例如：

```python
_drop_compaction_replayed_cron_users(prefix_messages: list[dict]) -> list[dict]
```

只删除同时满足以下所有条件的**后出现** user 行：

1. 是 `_is_real_user_message()`，不是 `context_anchor`、max-iteration summary request 或其他控制行；
2. 在它之前存在 `compaction_summary` context anchor；
3. 在该 anchor 之前已存在一条真实 user；
4. 两条 user 的规范化正文完全相同（现有 `_normalized_message_text()`：只折叠空白，不改写正文）；
5. 两条消息均属于同一个 execution prefix，而不是 prefix 之后的 WebUI follow-up suffix。

该规则只丢弃恢复副本，保留首次出现的原始 prompt、anchor、assistant/tool 轨迹和最终回复。若一次 execution 多次压缩，按顺序重复应用，最终每份初始 prompt 在 projection 中只保留第一次真实出现。

### 3.2 为什么边界必须是 execution prefix

相同正文可能是用户刻意重试，不能作为全局去重依据。Cron session 在 `cron_execution_ended_at` 之后允许普通 WebUI follow-up；这些 suffix 属于另一轮业务意图，即使文本相同也必须保留。

现有 `cron_execution_prefix_and_suffix(session)` 已提供这一可信分界：

```text
execution prefix  -> 可做 compaction-replay 投影去重
follow-up suffix  -> 字节与顺序均保持不变
```

所有调用方必须先切分，再只将 prefix 传给上述去重 helper；禁止将完整 session 直接交给该 helper。

### 3.3 代码落点

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `integration/crons/hooks.py` | 在 `normalize_cron_manifest_messages()` 中增加一个显式参数，例如 `collapse_execution_replayed_users=False`；实现上述纯 helper。默认仍为 `False`。 | 把 Cron 专属语义留在 integration 层，避免普通 session 或仅用于展示的调用被隐式改写。 |
| `integration/crons/session_bridge.py` | 在 `reconcile_cron_session_transcript()` 对已切出的 `merged_prefix` 调用 `normalize_cron_manifest_messages(..., collapse_execution_replayed_users=True)`；首次 materialization 的 `msgs` 也启用。 | 两条路径都会持久化 Cron sidecar，必须得到同一规范化结果。 |
| `integration/crons/hooks.py` | `settle_materialized_cron_session()` 已把 prefix/suffix 分开；对 prefix 启用该参数，保存时重新拼接 untouched suffix。 | 防止错误的 `turn:2` 被结算或影响 follow-up 的 next turn key。 |
| `integration/session_manifest/manifest.py` | 不在完整 merged transcript 上做删除。若需要一致的 display view，先依据 `cron_execution_ended_at` 切 prefix/suffix，再仅规范化 prefix；没有可信边界时 fail closed，保留原文。 | Manifest GET 是只读投影，且不能把 follow-up 误删。 |

`api/routes.py`、`api/streaming.py`、前端和 Agent 运行时均不应为首个修复而改动。该补丁遵守 `integration/` 优先原则，核心接缝只读取既有 sidecar 和 execution boundary。

### 3.4 伪代码

```python
def _drop_compaction_replayed_cron_users(prefix_messages):
    kept = []
    real_users_before_anchor = []
    saw_compaction_anchor = False

    for message in prefix_messages:
        if is_context_anchor(message) and message.get("_hermes_scaffold_kind") == "compaction_summary":
            saw_compaction_anchor = True
            kept.append(message)
            continue

        if not _is_real_user_message(message):
            kept.append(message)
            continue

        text = _normalized_message_text(message)
        if saw_compaction_anchor and text and any(
            _normalized_message_text(prior) == text
            for prior in real_users_before_anchor
        ):
            continue  # 仅删除本 execution 内的 compaction 恢复副本

        kept.append(message)
        if not saw_compaction_anchor:
            real_users_before_anchor.append(message)

    return kept
```

实现时不能直接照抄该伪代码：需要支持多次 anchor、保留每个 anchor 前已有的真实 user 集合，并先移除既有的 max-iteration 内部请求。测试必须固定这些细节。

## 4. 回归测试与验收

### 4.1 红测：精确绑定现场形态

在 `integration/tests/crons/test_manifest_turns.py` 增加最小脱敏 fixture。它必须包含：

1. 初始 Cron prompt `P`；
2. 一个 `compaction_summary` anchor；
3. 至少一个 assistant/tool 尾部；
4. 同一正文 `P` 的恢复 user；
5. 最终 assistant 交付。

修复前断言失败：归一化后有两个 `P`，并被标为 `turn:1`、`turn:2`。修复后应断言：

- 只剩一个可见 `P`；
- 该消息是 `turn:1`；
- anchor、工具尾部和最终 assistant 均保留；
- `_message_turns()` 只返回 `turn:1`；
- 同一工具/最终交付只对 `turn:1` 产生 Artifact decision。

### 4.2 必须保留的反向测试

| 场景 | 预期 |
| --- | --- |
| 没有 compaction anchor 的相同 user | 两条均保留。 |
| anchor 后不同正文的多条真实 user | 均保留。 |
| 已结束 execution 后的 WebUI follow-up 与初始 prompt 相同 | suffix 保留并获得下一 turn key。 |
| 两次 compaction 后同一 prompt 出现三次 | projection 只保留首条；不会留下键跳号。 |
| max-iteration summary request | 按现有规则移除；不能被当成重复 prompt 的依据。 |
| 缺失/无效 `cron_execution_ended_at` | 不启用 prefix 去重；保留消息并使 follow-up prepare 继续 fail closed。 |
| 普通 WebUI、CLI、messaging session | 完全不调用 Cron helper，内容不变。 |

### 4.3 验证顺序

1. 先新增红测，确认未修复分支失败于两个相同 user/两个 turn。
2. 实现纯 helper 并使上述红测通过。
3. 运行：

   ```bash
   ./scripts/test.sh integration/tests/crons/test_manifest_turns.py -q
   ./scripts/test.sh integration/tests/crons/test_session_bridge.py -q
   ```

4. 运行紧邻的 Manifest/streaming 回归测试；最后运行完整 `./scripts/test.sh`。
5. 使用隔离的 `HERMES_HOME` 与 `HERMES_WEBUI_STATE_DIR` 构造一个会触发两次压缩的 Cron job，验证 Agent 原始 transcript 仍可恢复任务，而 WebUI sidecar/Manifest 仅显示一个初始 user turn。
6. 手工检查已结束 Cron session 的“查看、刷新、打开 Manifest、发送同文本 follow-up”四种路径；最后一项应保留为新 turn。

## 5. 历史会话处理

首个补丁不批量写入历史 sidecar 或 Agent `state.db`。

- 下次读取/物化命中同一历史 Cron session 时，可在既有 sidecar 更新路径中幂等地写入规范化 projection；不触碰 Agent 原始行。
- 仅 Manifest GET 的只读路径不得保存；若没有侧边车或 execution boundary，宁可展示旧数据也不猜测删除。
- 如需主动清理已物化历史 sidecar，另设显式维护命令：先 dry-run 输出 session 数、命中重复数和不确定数，再逐个保存已验证的 projection；该维护命令不属于本修复 PR。

## 6. 后续：Agent 侧语义增强（独立 PR）

文本完全相等是当前可用且受 execution-prefix 限制的兼容修复，但它仍是投影层推断。建议在 Hermes Agent 后续单独提供 durable 语义，例如给由 `_ensure_last_user_message_in_tail()` 恢复的行写入一个明确的 provenance/scaffold marker，同时保持其对模型的 `role=user` 语义。

WebUI 只有在 Agent 版本稳定且旧库兼容策略明确后，才可优先按该 marker 过滤；在 marker 缺失的历史运行上继续使用第 3 节的严格 prefix 规则。该 PR 不得改变 provider payload、`state.db` 的任务连续性或把恢复行隐藏为 context anchor。

## 7. 提交拆分与文档路由

建议拆为两个逻辑提交：

1. `修复：折叠 Cron 压缩恢复的重复用户消息`：`integration/crons/hooks.py`、`integration/crons/session_bridge.py`、`integration/session_manifest/manifest.py` 与对应测试。
2. `文档：记录 Cron 压缩恢复消息去重约束`：本方案、`integration/README.md` 的 Cron transcript 段落，以及必要时 `integration/CHANGELOG.md`。

首个提交可独立回滚：回滚后只恢复旧 projection 行为，不影响 Agent 执行、workspace、job 定义、HTTP schema 或 Manifest 数据库 schema。

PR 必须附带 Contract Routing：运行时状态层为“Agent canonical transcript → WebUI Cron sidecar projection → Manifest turn projection”；证据为脱敏样本回放、红测、相关 pytest 和隔离 Cron 端到端验证。根目录 `CHANGELOG.md` 不改动；若对外记录 Fork 行为，更新 `integration/CHANGELOG.md`。

## 8. 完成标准

只有同时满足以下条件才可标记为完成：

1. 精确复现样本在未修复时失败、修复后通过。
2. 同一 compaction-replayed Cron prompt 在 WebUI sidecar、聊天 API 和 Manifest 中只形成一个真实 user turn。
3. Agent 原始 `state.db` 行、compaction anchor 和工具轨迹未被修复代码删除或改写。
4. execution suffix 中的同文 follow-up 仍存在，且键连续、Artifact 归属正确。
5. 无可信 execution boundary 时不删除任何 user 行。
6. 相关测试、隔离端到端验证和手工刷新/继续对话检查均通过。
