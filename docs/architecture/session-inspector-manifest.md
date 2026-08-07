# Session Inspector Manifest

Session Inspector Manifest 是从会话活动派生出的轻量索引，用于右侧 Workspace Inspector 的 Tasks、Artifacts、References 面板，以及聊天区每轮的 artifact chips。它不是 transcript、执行 journal，也不是 workspace 全量文件列表。

对外 HTTP/SSE 字段见 [session-manifest-api.md](./session-manifest-api.md)；Artifacts 的工具证据、路径过滤、持久化和 read-repair 见 [session-manifest-artifacts.md](./session-manifest-artifacts.md)。

## 数据语义

| 数据 | 含义 | 明确排除 |
| --- | --- | --- |
| Tasks | 当前轮 `todo` 工具产生的最新任务快照 | 历史流水、助手正文中的列表 |
| Artifacts | 当前会话通过明确成果证据创建、修改或交付的文件/技能 | 搜索命中、目录列表、输入文件、跨字段推断 |
| References | 明确成功 `skill_view` 的 canonical 技能 | 文件读取、搜索、列目录、助手普通提及 |
| Turns | 按真实 user 消息划分的 per-turn artifacts/skill references 视图 | transcript 或完整执行历史、Agent internal scaffold、model-only context anchor |

同一资源在一个 manifest 结果中只保留一个主归类，优先级为 `artifacts > references`。缺失字段保持为空或跳过，不从相似字段推断、复制或补全。

## Artifact 证据边界

Artifacts 可来自成功写入工具的结构化参数/diff、成功 terminal 的受控输出操作数、显式 `MEDIA:`、成功 skill mutation，以及当前轮最后一条 assistant 中经严格验证的 workspace 文件。工具调用 start 本身不构成证据；必须有同一工具调用的成功完成结果。成功文件读取路径仅作为同 turn 的瞬态 read evidence，抑制同路径 `assistant_prose` 弱候选；不公开、不持久化，也不抑制强工具/MEDIA artifact。中间 assistant prose 不提取路径。

最终 assistant 候选必须位于 workspace、真实存在且可预览；不存在的文件不会因正文声称已交付而进入 Manifest。裸文件名按当前 turn 强证据、此前 turn 已确认成果、workspace 根目录的顺序做唯一 exact-basename 解析；同层歧义时跳过，不从自然语言目录上下文补全。因而后续纯问答 turn 的最后一条 assistant 若明确列出可唯一解析的既有文件，该文件可作为该 turn 的 `assistant_prose` artifact。Terminal 不扫描 stdout、目录列表、heredoc 源码或整个 workspace。

具体工具白名单与算法只有 [session-manifest-artifacts.md](./session-manifest-artifacts.md) 是权威来源。

## 状态生命周期

- 流式过程中，聊天 SSE 可发送 `manifest_delta`，Inspector 乐观合并。
- SSE delta 不写入 transcript，也不替代持久化 Manifest。
- 本轮 `done` 后，前端重新请求 `GET /api/session/manifest` 并覆盖乐观状态。
- 聊天区每轮成果 chips 只使用 GET 返回的 `manifest.turns[].artifacts`，不直接使用 SSE delta。
- Artifacts 的持久化权威来源是 profile-aware artifact store；transcript/tool/prose 只用于受控 backfill 或 empty-decision read-repair。
- Store artifact 没有同 key user anchor 时仍保留在顶层 `artifacts`，并列入 `diagnostics.orphan_turn_keys`；不得追加为正常 `turns[]`。

## UI 消费

- **Tasks**：Workspace Inspector 与 Control Center Todos。
- **Artifacts**：Workspace Inspector 全会话聚合，以及聊天区 per-turn chips。
- **References**：Workspace Inspector；聊天区当前不展示 per-turn references。
- **Expired**：具有历史 provenance 但当前不可预览的条目可保留为 `status: "expired"`，不可点击预览。

## 核心不变量

1. Manifest 是派生索引，不驱动 Agent，也不替代 transcript/journal。
2. 工具来源只接受成功 completed 事件；References 只来自明确成功的 `skill_view`。
3. 每个 turn 只从最后一条 assistant prose 提取 artifact 路径；中间 assistant prose 不提取。
4. 不扫描整个 workspace，不把搜索命中或目录子项提升为成果/参考。
5. SSE 是乐观态；`done` 后 GET 是展示权威态。
6. Store 中已有非空 artifact decision 的 turn 不重扫、不覆盖；empty decision 只允许严格同轮修复。
7. 重放同一稳定 `tool_call_id` 不得产生第二次执行或跨 turn artifact 归属。
8. 正常 `turns[]` 必须有 user anchor；聊天区 assistant 容器使用该 user 的 `_turn_key` 作为 `data-turn-key`。混合 transcript 的无 key user 只进入 diagnostics，不由 Manifest GET 猜号。
9. 带 `_hermes_message_class: internal_scaffold` 的 Agent 控制消息，以及保留原始正文并带语义字段的 `context_anchor` 不产生 turn；其间的 assistant/tool/MEDIA 仍归属前一个真实 user turn。
