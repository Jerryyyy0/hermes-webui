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

## 5. 与 Manifest Artifact 的交界

默认 workspace 不限制用户显式指定的绝对路径；Agent 继续按任务 cwd 解析相对路径，也不把
绝对路径重写进 session workspace。一个文件是否成为可预览 Artifact、Artifact 根如何在
SSE → persist → GET → preview 间保持一致、以及 `workspace_root + path` 的存储和安全约束，
均由 [Session Manifest Artifacts 实现](session-manifest-artifacts.md) 唯一维护。

该交界不新增数据库表或 Manifest wire 字段。`done` 后 GET 是聊天 artifact chip 的最终权威，
流式 SSE 只是 Inspector 乐观态。

## 6. 验证与可观测性

通过仓库测试脚本运行 Manifest、受管 workspace 与 profile terminal 环境测试。诊断仅记录
`artifact_workspace_root`、候选/接受/根外拒绝计数和持久化状态；不得记录完整 prompt、
Memory、Profile、文件内容或未经成果证据支持的任意路径。
