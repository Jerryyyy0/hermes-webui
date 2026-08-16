# Workspace 默认目录与 Turn Artifact 兼容修复方案

- **状态：** Implemented
- **创建日期：** 2026-08-16
- **范围：** WebUI 浏览器会话；`api/streaming.py`、Session Manifest、workspace preview

## 1. 问题

WebUI 为新会话创建受管工作区时，实际会话工作区通常是：

```text
DEFAULT_WORKSPACE = /Users/wzq/workspace
session.workspace = /Users/wzq/workspace/sessions/<session_id>
```

运行时已经将 `session.workspace` 传给 Agent：

- `TERMINAL_CWD` 是相对路径工具调用的默认 cwd；
- 每轮 user message 带有 `[Workspace::v1: /absolute/path]`；
- `system_message` 提示模型把该标签作为默认工作目录。

但持久 Memory / USER Profile 可以包含历史的、硬编码的 `/workspace` 或
`DEFAULT_WORKSPACE` 路径。它们在当前 Agent 的 cached prompt 中位于 WebUI
workspace 规则之后，模型可能因此把未指定输出位置的文件写到会话工作区外。

这不一定是文件写入失败：Agent 的文件工具应继续允许用户本轮显式指定的绝对路径。
真正的用户可见问题是 Turn Artifact：目前普通 file artifact 的 preview gate 以
`session.workspace` 为根。写到 `/Users/wzq/workspace/<file>` 的文件虽然仍在全局
workspace 内，但不在 `/Users/wzq/workspace/sessions/<session_id>` 内，完成结算后会被
过滤，聊天区 artifact chip 与 Session Inspector 都不会显示它。

## 2. 决策

本方案定义两个不同概念，不能再混用：

| 名称 | 权威值 | 用途 |
| --- | --- | --- |
| 会话默认工作区 | `session.workspace` | 用户未指定路径时创建文件、相对文件工具路径、terminal 默认 cwd |
| Artifact 归属根 | 当 `session.workspace` 位于 `DEFAULT_WORKSPACE` 下时为该默认根；否则为 session 自身根 | 判断当前 turn 已成功写入的文件是否可以作为 WebUI Artifact 展示与预览 |

因此，用户本轮明确指定的绝对路径可以越过 `session.workspace`；这不是权限错误。
只有处于 Artifact 归属根外的结果不进入普通 file artifact。该文件仍可写入并由 Agent
在回答中报告，但不会获得聊天 Artifact chip 或 Workspace Inspector 预览。

本方案不增加 manifest wire 字段，也不把 `GET /api/session/manifest` 变成全盘扫描或
任意绝对路径文件浏览接口。

## 3. 目标与非目标

### 目标

1. 用户未提供目标位置时，文件默认直接创建在 `session.workspace` 根目录。
2. 用户本轮明确指定的绝对路径保持原样，可位于会话工作区外。
3. 对于位于 `DEFAULT_WORKSPACE` 下的会话，位于该默认根下、且有当前 turn 成功工具
   证据的文件，即使位于 `session.workspace` 外，也能稳定出现在 SSE、完成后的
   Manifest GET 和 turn chip；默认根外会话仍以自身根隔离。
4. 同一文件在实时 SSE 与最终 GET 中使用相同的根目录和相对路径，不出现“流中可见、
   刷新后消失”或“存储有记录、预览打不开”。
5. 继续保持 Manifest 是会话活动的派生索引：不把搜索命中、目录列表、读取行为或
   assistant 的中间文本升级为 Artifact。

### 非目标

- 不限制 Agent 对用户明确路径的读写权限；尤其不以 `target.relative_to(session.workspace)`
  作为通用写入拒绝条件。
- 不自动把 workspace 中已有文件、其他会话文件或历史文件批量加入当前 turn。
- 不展示 `DEFAULT_WORKSPACE` 外的本机文件为普通 file artifact。
- 不修改 Memory 或 USER Profile；这些内容的维护仍需明确用户授权。
- 不在 Manifest GET 中执行 backfill、read-repair 或工作区扫描。

## 4. 第一层：最终系统提示词的默认目录裁决

### 4.1 放置位置

在 `api/streaming.py` 的 `_webui_ephemeral_system_prompt()` 中，现有 delivery context
之后追加 workspace policy。`agent.ephemeral_system_prompt` 在普通主循环中会追加到
cached system prompt 后，因此它位于 Memory、USER Profile 与模型元信息之后。

调用处已经通过 `surface_context['workspace'] = s.workspace` 传入实际值；不得把
`<session.workspace>` 占位符原样发送给模型。

### 4.2 提示词正文

以下文本由服务端插入已规范化的 `s.workspace`，并且必须是 ephemeral prompt 的最后一段：

```text
Workspace resolution policy — applies to this request:

The current session workspace is: <resolved session.workspace>

This workspace is the default location for newly created files when the user
has not specified a target path. It is not a global filesystem access boundary.

Precedence for choosing a write target:
1. An explicit path named by the user in the current request. It may be outside
   the session workspace; honor it and do not rewrite it into the workspace.
2. The current session workspace above.
3. Never infer a default output directory from memory, prior conversation,
   examples, or a hardcoded /workspace path.

If the user does not name a destination, write directly under:
<resolved session.workspace>/<filename>

Do not use /workspace/<filename> unless the user explicitly requests that path.
```

### 4.3 代码边界

新增一个纯 helper，例如 `_webui_workspace_policy_prompt(surface_context)`，从
`surface_context['workspace']` 读取绝对路径；没有合法 workspace 时返回空字符串。
`_webui_ephemeral_system_prompt()` 将其作为 `parts` 的最后一项追加。

此层是模型决策引导，不是安全边界。它的职责是修复“未指定路径时被旧记忆误导”的行为，
而不是拒绝跨工作区变更。

## 5. 第二层：执行与 Artifact 的同根兼容

### 5.1 执行路径保持现有兼容性

以下已有行为应保留并覆盖回归测试：

1. `api/streaming.py::_build_agent_thread_env()` 将 `TERMINAL_CWD` 设为
   `str(s.workspace)`；profile 配置中的旧 cwd 不能覆盖它。
2. Agent 文件工具对相对路径按任务 cwd 解析。
3. Agent 文件工具对绝对路径仅规范化，不把它重写为 session workspace 内路径。

这意味着默认相对写入和显式跨工作区写入可以共存。不要在 Agent 侧引入“所有绝对路径
必须位于 session workspace”这一类 containment guard。

### 5.2 新增唯一的 Artifact 根决策函数

在 `api/session_manifest.py` 或一个无循环依赖的 workspace helper 中新增唯一入口：

```python
def artifact_workspace_root_for_session(session) -> Path:
    """Return the normalized root eligible for ordinary file artifacts."""
    default_root = resolve_trusted_workspace(None)
    session_root = Path(session.workspace).expanduser().resolve()
    return default_root if session_root.is_relative_to(default_root) else session_root
```

实现必须使用当前运行时的 `api.config.DEFAULT_WORKSPACE`，而非硬编码 `/workspace`、
启动 cwd 或从 Memory 推断的目录。若根目录无法规范化或不可信，Artifact 采集应 fail closed：
不发布普通 file artifact，并记录可诊断的结算失败；不能退化为任意绝对路径预览。

`session.workspace` 仍是默认写入目录；该 helper 只影响 Artifact 资格与 preview。对于
历史或外部 workspace 会话，helper 回退到 session 自身根，避免把不相关的根目录合并。

### 5.3 同一权威值必须贯穿全部阶段

将当前直接传入 `Path(str(s.workspace))` 或 `Path(str(session.workspace))` 的
Artifact 调用替换为同一个 `artifact_workspace_root_for_session(...)` 结果。

| 阶段 | 当前入口 | 变更要求 |
| --- | --- | --- |
| 工具完成 SSE | `extract_manifest_delta_from_tool_event()` 的 streaming 调用 | 使用当前 stream 的 artifact root |
| turn 完成 SSE | `extract_manifest_delta_from_turn_reconcile()` 的 streaming 调用 | 使用相同 artifact root |
| transcript reconcile | `extract_turn_artifact_entries_for_manifest()` | 使用 session artifact root |
| 持久化 preview gate | `_persist_turn_artifact_paths()` + `filter_existing_turn_artifact_entries()` | 使用相同 artifact root |
| GET 投影 | `build_session_manifest()`、`_row_to_wire()` | 查询与预览按同一 artifact root |

这是一条端到端不变量：`input → normalize → SSE → persist → GET → preview` 使用同一个
规范化根。不得只放宽 `_resolve_manifest_path()`；那会制造不同阶段的可见性分歧。

### 5.4 存储与 wire 不新增字段

现有 store 身份已经含有 `workspace_root + path`。将本次 file artifact 的
`workspace_root` 记录为 Artifact 归属根，`path` 保持该根下的 POSIX 相对路径即可，例如：

```text
workspace_root = /Users/wzq/workspace
path           = sessions/<session_id>/brief.md
```

或：

```text
workspace_root = /Users/wzq/workspace
path           = 北京天气简报.md
```

现有 integration workspace preview 已以 `DEFAULT_WORKSPACE` 为根；因此同根路径可以
投影为现有 `/api/integration/workspace/file` 使用的相对路径，无需 `scope`、
`external_file` 或新的 wire schema。

`DEFAULT_WORKSPACE` 外的绝对路径必须在现有 file preview gate 被排除；不要在 wire 中
回退为裸绝对路径。

## 6. 证据与安全边界

Artifact 资格不是“文件位于根目录”这一项就足够。仍需同时满足：

1. 当前 turn 的成功 completed 工具事件，或当前 turn 最后一条 assistant 的严格验证成果；
2. 路径位于 `artifact_workspace_root_for_session(session)` 内；
3. 文件当前真实存在、是普通文件、非 cruft、可预览；
4. turn key 与当前真实 user anchor 一致；
5. store 写入成功后，`done` 后 GET 的结果才是聊天 chip 的最终权威来源。

不允许从 terminal stdout、`ls`、搜索结果、读取结果、heredoc 或整个 workspace 推断成果。
这保留 [Session Inspector Manifest](session-inspector-manifest.md) 与
[Manifest Artifacts](session-manifest-artifacts.md) 的证据模型。

## 7. 实现落点

1. 在 `api/streaming.py` 新增末尾 workspace ephemeral policy helper 和单元测试。
2. 新增 `artifact_workspace_root_for_session()`，并为默认根、受管 session 子目录和
   无法规范化根写入单元测试。
3. 将 streaming 的 tool-complete delta、turn-complete delta、完成/错误/取消结算统一
   传入 Artifact 根。
4. 将 `api/session_manifest.py` 的 transcript 提取、reconcile、`build_session_manifest()`
   与 store root 判定统一改用该根。
5. 核对 `api/session_manifest_store.py` 的 `workspace_root` 写入与
   `effective_manifest_workspace_root()` / integration projection 对同一根保持一致。
6. 在 WebUI 中手动验证：实时 chip、完成后自动 GET 刷新、刷新页面后重开 session、
   preview 点击，四者均指向同一文件。

## 8. 验收测试矩阵

所有 pytest 通过仓库脚本运行：

```bash
./scripts/test.sh \
  tests/test_session_manifest.py \
  tests/test_session_manifest_store.py \
  tests/test_session_manifest_contract.py \
  tests/test_session_manifest_replay.py \
  tests/test_session_managed_workspace.py \
  tests/test_issue1913_workspace_prefix_sentinel.py \
  tests/test_profile_terminal_env.py
```

已新增以下关键行为的自动化覆盖；其余场景应继续由相邻 Manifest 回归测试保护：

| 场景 | 写入目标 | 期望 |
| --- | --- | --- |
| 无路径的新文件 | `session.workspace/report.md` | 成为当前 turn Artifact；可 SSE、GET 与 preview |
| Memory 含 `/workspace` | 用户只说“生成报告” | 仍写入 `session.workspace`，并成为 Artifact |
| 显式全局 workspace 文件 | `DEFAULT_WORKSPACE/report.md` | 保持原绝对路径；成为当前 turn Artifact；GET 后仍可预览 |
| 显式另一受管 session 子目录文件 | `DEFAULT_WORKSPACE/sessions/other/a.md` | 只在当前 turn 有成功工具证据时显示；不扫描或自动发现 |
| 显式 workspace 外文件 | `/Users/wzq/Documents/a.md` | 写入不被该方案阻止；不成为普通 file Artifact |
| 失败、取消、未完成工具 | 任意路径 | 不产生 Artifact；不能伪装为 empty 成功 |
| 两个并发 session | 不同 session workspace | SSE/DB/turn key 不串线；各自只显示自己的成功证据 |

## 9. 迁移与回滚

本方案不改变数据库表结构或公开 wire schema。新决策仅影响后续 turn 的
`workspace_root` 与路径相对化。历史 store rows 按其已有 `workspace_root` 继续读取，
不由 GET 迁移或回填。

回滚时恢复 session-workspace 作为 Artifact 根即可；历史以 `DEFAULT_WORKSPACE` 写入的
记录仍能被现有 integration-root 投影读取，不需要删除或改写 `session_manifest.db`。

## 10. 可观测性

在 stream diagnostic / turn journal 中记录非敏感字段：

```text
artifact_workspace_root
artifact_candidate_count
artifact_accepted_count
artifact_rejected_outside_root_count
artifact_persistence_status
```

不要记录完整 system prompt、Memory、USER Profile、文件内容、凭据或未获成果证据支持的
任意路径。出现 Artifact 缺失时，可借此区分：模型选错默认路径、成功工具证据缺失、
preview gate 拒绝、turn key 绑定失败或 store 持久化失败。
