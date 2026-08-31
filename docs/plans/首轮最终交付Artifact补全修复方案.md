# 首轮最终交付 Artifact 补全修复方案

## 1. 背景与复现证据

会话 `d51f6602989b` 暴露了一类「非空决策不完整」问题：首个真实用户轮次
`turn:1` 已持久化两份中间 Markdown 成果，但在该轮最终 assistant 消息中明确以
`MEDIA:` 交付的 V2 HTML 与 Word 没有写入同一轮的 Manifest 决策。

后续两轮是正确的，不能通过迁移或重绑来修复：

| 轮次 | 真实用户意图 | 应保留的成果归属 |
| --- | --- | --- |
| `turn:1` | 生成商业计划书 | 两份 Markdown，以及首次交付的 V2 HTML、V2 Word |
| `turn:2` | 询问 HTML/Word 文件位置 | 再次明确交付的同一份 V2 HTML、V2 Word |
| `turn:3` | 格式化 Word 文档 | `_格式化.docx` |

同一文件在多个真实 turn 出现是允许且必要的：Artifact Store 的身份键含
`turn_key`，聊天区也按 `turns[].artifacts` 读取。修复目标是**向 `turn:1`
增量补齐**两条交付记录，而不是从 `turn:2` 或 `turn:3` 删除、移动或重命名记录。

### 已确认事实

1. 完整 transcript 中 `turn:1` 的最终 assistant 消息明确包含两个本地
   `MEDIA:` 路径。
2. 以完整 transcript 和存在的工作区文件回放当前提取器时，`turn:1` 会提取出
   这两个 V2 文件及既有两份 Markdown。
3. 当前 Manifest 的 `source` 为 `db`；`turn:1` 的非空 Store decision 仅含两份
   Markdown。
4. `GET /api/session/manifest` 规定为只读，不会对非空 decision 做历史回填。
5. `turn:2` 与 `turn:3` 是消息截断后的可见真实用户锚点，不是 orphan，也不是
   本次修复对象。

当前捕获不能单独证明哪一个历史 worker/续跑回调漏调了结算入口；它只证明
`turn:1` 在最终交付之后没有完成一次包含最终证据的增量结算。实现前必须用
server 日志、turn journal 或可复现测试确认具体入口。

## 2. 契约与不变量

本方案遵循：

- [`docs/api/session-manifest-api.md`](../api/session-manifest-api.md)
- [`docs/architecture/session-manifest-artifacts.md`](../architecture/session-manifest-artifacts.md)

状态所有者：

| 状态 | 所有者 | 本方案是否直接修改 |
| --- | --- | --- |
| 完整 transcript、tool calls | Session sidecar | 否，只读作证据 |
| Artifact 权威投影 | `session_manifest.db` | 是，仅追加通过 gate 的缺失行 |
| SSE live manifest | 当前 stream 内存状态 | 是，正常完成时发 delta；历史修复不伪造旧 SSE |
| 工作区文件 | Session workspace | 否，绝不复制、移动、删除或覆盖 |

必须保持以下不变量：

1. 使用 stream 启动时绑定的 `turn_key`；不得由 `user_message_count`、当前消息数、
   `stream_id` 或最后一条用户消息重新推导。
2. 同一文件可在多个 turn 出现；同一 turn 内按 canonical path 去重。
3. 历史补全只接受同一 turn 内的受控强证据：成功 completed 的结构化文件 mutation
   （`write_file`、`create_file`、`edit_file`、`patch`、`apply_patch` 及既有白名单中的
   等价工具）给出的参数或 diff 路径；零退出 terminal 中既有解析器能静态确认的**输出**操作数；
   或当前 turn 最终 assistant 明确交付的本地 `MEDIA:` 路径。所有候选必须在该 Artifact
   row 自己的 workspace root 内可安全预览。用户附件、普通正文提及、目录列表、terminal stdout
   与命令中的输入/依赖路径均不得成为历史修复候选。
4. 历史修复仅 `upsert` 缺失 path；不得使用 `replace_manifest_turn_records()` 删除该
   turn 已有的非空 records，更不得触碰其它 lineage/profile/workspace root/turn。
5. 无法加载完整 transcript、找不到稳定 user anchor、路径不安全、不存在、或证据不够
   明确时跳过并报告；不能将不确定性视为可修复。
6. Manifest GET 保持只读；不得在打开会话、SSE 重连或前端 refresh 时隐式修复。
7. 任何补修候选若在目标 turn 完成后曾被另一真实 turn 成功写入或修改同一 canonical
   path，则默认视为版本冲突并跳过；没有不可变版本证据时不得把当前文件反向归属给较早 turn。

## 3. 根因模型

逻辑用户轮次可能包含多个工作阶段：先创建工作稿、等待或恢复后台任务、再生成最终文件并
发送最终 assistant 交付。若首次阶段已写入一个非空 Manifest decision，而最后交付阶段没有
对同一稳定 `turn_key` 再次结算，Store 就停留在中间集合。

由于非空 Store decision 是 GET 的权威来源，后续只读构建不能用 transcript 覆盖它；这避免了
错误的跨 turn 推断，却也使已丢失的同轮最终交付持续不可见。

要修复的是结算的生命周期，不是 Manifest 的读取逻辑：

```text
同一 turn 的工作稿写入
  → 首次非空 decision
  → 后台/续跑追加最终 assistant + MEDIA
  → 同一 turn 的增量结算（缺失：本次 bug）
  → Store 含工作稿 + 最终交付
```

## 4. 实施设计

### 4.1 统一的增量结算入口

保留 `api/streaming.py` 的 `_persist_turn_artifact_paths()` 作为唯一 Store 写入入口，
但把其调用语义明确为「每次某个已绑定 turn 的 durable transcript 获得终态 assistant
交付时都可安全重入」。

实现要求：

1. 在写入最终 assistant 消息并使完整 transcript durable 后调用该入口。
2. 在同一 session lock 中完成 `stream_id` 当前性校验、绑定 `turn_key` 校验、完整 transcript
   读取、同 turn 证据提取与 `upsert_manifest_records()`；任一步发现 worker 已过期、key 冲突或
   transcript 不完整时不写入。不得让后台完成在新轮次已启动后向旧轮次补写成果。
3. 入口从完整、同 turn slice 提取强证据与最终交付证据，经过既有路径安全和文件存在性
   gate 后，调用 `upsert_manifest_records()`。
4. 已有非空 decision 不得短路；`upsert` 必须将新 path 并入原 decision，而不是只在
   decision 为空时执行。
5. 不同的最终来源共用这条入口：普通 Agent streaming、Gateway streaming、后台任务/委派
   恢复、以及任何会把最终 assistant 内容附加到已有真实 turn 的路径。
6. 成功追加后，SSE `manifest_delta` 只发送本次新增、已绑定到当前 `turn_key` 的 rows；前端按
   既有合并规则累积。该 stream 的 `done` 后以 GET Manifest 覆盖为完整权威集合，不能要求
   每个 delta 重放整轮或整会话成果。历史补修无需向已结束的 stream 补发事件。

此处需要逐一审计所有「追加 assistant 消息」入口，而不是只修复普通 streaming：

| 入口类别 | 必须验证 |
| --- | --- |
| 普通 Agent streaming | 保存最终 transcript 后再结算；同 turn 重入会追加而非覆盖 |
| Gateway streaming | 与普通路径使用同一稳定 key 和完成边界 |
| `delegate_task` / process 等后台完成 | 恢复回原 turn 时，最终交付追加后会触发结算 |
| 成功、error、cancel | 只有成功完成的 final delivery 可追加；error/cancel 不把未验证候选写成成果 |
| 新用户轮次再次交付同文件 | 新 key 可以新增该 path，旧 key 保留 |

### 4.2 历史非空 decision 的受控补全

已新增维护脚本 `scripts/repair_incomplete_manifest_turns.py`，而不是改变 GET 或前端刷新流程。
脚本只处理显式指定 session，默认 dry-run：

```bash
./scripts/test.sh tests/test_session_manifest.py tests/test_session_manifest_store.py -q
python3 scripts/repair_incomplete_manifest_turns.py \
  --session-id d51f6602989b \
  --turn-key turn:1

# 人工核对输出的 stable key、logical workspace root、profile、候选路径及证据后：
python3 scripts/repair_incomplete_manifest_turns.py \
  --session-id d51f6602989b \
  --turn-key turn:1 \
  --apply
```

脚本流程：

1. 载入完整 session sidecar，而不是 API 的截断 display slice；缺少完整内容即失败关闭。
2. 解析指定真实 user anchor，校验其 `turn_key` 与 Store 的
   `(lineage_key, profile, workspace_root)` 身份范围。
3. 从完整同 turn slice 提取受控候选，且复用正常结算的证据资格与路径 gate：
   - 已配对且成功 completed 的结构化 mutation 的参数或 diff 路径；
   - 零退出 terminal 中既有静态解析器识别的输出操作数；
   - 该 turn **最终真实 assistant 消息**中的显式 `MEDIA:` token。

   terminal stdout、shell 输入、依赖路径、目录列举和普通 assistant prose 一律不参与。候选
   还必须通过 media 路径规范化、受保护路径与存在性 gate。
4. 对每个候选检查目标 turn 之后的完整 transcript/tool-call 证据：若另一真实 turn 成功写入或
   修改相同 canonical path，记为 `version_conflict` 并跳过。只有能证明不存在后续覆盖的路径才
   能进入写入集合；脚本不得用当前文件内容猜测历史版本。
5. 先按 canonical path 对同一 turn 的所有候选去重，再计算
   `deduped_candidate_paths - persisted_paths`；同一文件同时被 mutation、terminal 输出和
   `MEDIA:` 证明时只生成一条待写 row，并按既有 source 优先级保留 provenance。候选为空时
   报告 `no_change`，不写 DB。
6. `--apply` 时在单个 SQLite 事务中对去重后的缺失行 `upsert`，并在提交前以相同
   `(lineage_key, profile, workspace_root, turn_key, record_kind, path)` identity 查询验证新增行
   已存在；验证失败即回滚该事务。保留原有 records，并输出 `added/unchanged/rejected` 的
   结构化摘要。
7. 提交后重新构建 Manifest 仅作为读取健康检查：若失败，明确报告“写入成功、读取验证未完成”
   并退出非零，不伪称回滚或成功完成；不得执行猜测性的二次删除。

对于本案，该脚本预期只向 `turn:1` 添加：

```text
超级智能体SaaS订阅服务商业计划书-V2.0.html
超级智能体SaaS订阅服务商业计划书-V2.0.docx
```

`turn:2` 的同名 rows 和 `turn:3` 的 `_格式化.docx` 都不在该脚本的删除或重绑范围内。

### 4.3 可观测性

在共享结算入口添加结构化、非敏感日志/turn-journal 事件，至少包含：

- `session_id`、`stream_id`、绑定的 `turn_key`；
- 结算原因（`initial`、`final_delivery_append`、`error`、`cancel`）；
- 提取数、已有数、新增数、被安全 gate 拒绝数；
- 结算状态与失败 stage。

路径本身只应在现有受控调试日志级别出现；普通 INFO 不记录可能包含私有目录的完整路径。

## 5. 回归测试计划

测试必须先在修复前失败，再在修复后通过。回归 fixture 必须从已提供的 session capture
脱敏派生，而不是只手工构造相似场景；需保留其关键形状：截断的 display slice、可读取的完整
sidecar、一个真实 `turn:1` 的早期非空结算、同 turn 的最终 `MEDIA:` 双文件交付、后续真实
`turn:2` 重发，以及 `turn:3` 生成新文件。

| 用例 | 断言 |
| --- | --- |
| 同 turn 延后交付 | 早期两条 Artifact 已存在时，最终 HTML/Word 会追加到 `turn:1` |
| 跨 turn 重发 | 相同 path 同时出现在 `turn:1` 与 `turn:2`，不会互相覆盖 |
| 格式化派生文件 | `_格式化.docx` 只在 `turn:3` |
| 失败/取消续跑 | 未完成或不安全的交付不新增 Artifact，也不改写既有 decision |
| 普通与 Gateway | 两个后端均按相同 `stream_turn_key` 追加 |
| 历史 dry-run | 不改 DB，给出精确候选与拒绝原因 |
| 历史 `--apply` | 只新增两个缺失 path；第二次执行为 no-op；既有和其它 turn rows 不变 |
| 多来源去重 | 同一 canonical path 被 mutation、terminal 和 `MEDIA:` 同时命中时仅写一行；重复执行仍为 no-op |
| 历史版本冲突 | 相同 path 在后续真实 turn 被成功覆盖时，repair 报 `version_conflict` 且不写入 |
| 工具文件变更 | 成功 `write_file`/`patch` 的结构化路径、以及 terminal 的受控输出操作数可补全；stdout、输入和依赖路径不可以 |
| 安全边界 | assistant 的普通文本、输入附件、terminal 命令中的依赖/输入路径不能被历史脚本登记 |
| 截断保护 | 只有截断 session、没有完整 sidecar 时脚本失败关闭 |
| 陈旧 worker | 新 stream 已绑定或持锁验证失败时，旧 worker 不得对旧 turn 写入任何增量 |
| SSE 合并 | delta 仅含本次新增 row；done 后 GET 覆盖得到该 turn 的完整集合 |

完成代码后执行：

```bash
./scripts/test.sh tests/test_session_manifest.py tests/test_session_manifest_store.py tests/test_session_manifest_contract.py tests/test_session_manifest_replay.py -q
```

并在隔离的 `HERMES_HOME`、`HERMES_WEBUI_STATE_DIR` 下完成一次端到端验证：同一轮先落盘
工作稿、再交付 media；刷新会话后首轮 chip 仍显示全部成果；随后重发同文件和生成派生文件，
三个 turn 的展示分别与上表一致。

## 6. 发布、回滚与范围控制

1. 先合入运行时结算修复与回归测试；不要自动扫描或修改历史数据库。
2. 以 dry-run 在受影响会话上核对候选，记录 session、lineage、profile、logical root、turn 和
   添加路径数量；人工批准后才执行 `--apply`。
3. 每次 apply 前使用 SQLite 在线备份或运维既有备份流程保存 `session_manifest.db`；脚本自身
   不复制、移动或删除工作区文件。
4. 如需回滚，仅删除该次维护日志中明确新增的同一 identity rows；不得回滚 `turn:2`、`turn:3`
   或其它会话记录。
5. 本次不改 Manifest wire schema、前端 UI、文件预览接口或 Artifact 的安全资格规则；只修正
   同一真实 turn 的结算时机和受控历史补全。

## 7. 验收标准

- 给定本案完整 session，`turn:1` 显示两份工作稿和 V2 HTML/Word；`turn:2` 保留 V2
  HTML/Word；`turn:3` 仅显示格式化 Word。
- 顶层 `artifacts` 仍按现有去重规则展示，不影响 per-turn 重复归属。
- 实际写入 DB 的 identity 不跨 lineage、profile、workspace root 或 turn。
- refresh/GET 不产生数据库写入；历史修复只能经显式 `--apply`。
- 普通、Gateway 与后台续跑的终态交付均经过同一个增量结算边界。
