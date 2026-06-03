# 会话 Inspector Manifest：待办、成果与参考

本文档说明 WebUI 在**一次会话的多轮交互过程中**，如何从 Agent 的工具活动里归纳出右侧面板的三类信息，以及这些产品语义上的边界与限制。不展开具体前后端实现细节。

## 它是什么

**Session Manifest** 是会话活动的**派生索引**，供界面展示「当前会话里发生了什么」的摘要：

| 面板 | 含义 |
| --- | --- |
| **待办 Tasks** | 当前会话里 Agent 维护的最新任务列表状态（以最近一次 `todo` 工具结果为准）。 |
| **成果 Artifacts** | 当前会话所有轮次中通过**写入类工具**实际创建或修改过的文件，聚合去重展示。 |
| **参考 References** | 当前会话所有轮次中被 Agent **实际读取/打开**过的具体内容来源，主要是文件，聚合去重展示。 |
| **轮次 Turns** | 每一轮 user-agent 交互的派生分组，供聊天区在当前轮次下方展示本轮成果文件。 |

Manifest **不是**：

- 聊天 transcript 的权威副本（仍以会话消息为准）；
- Workspace 文件树的完整列表（**Files** tab 才是目录浏览）；
- 可回放、可审计的执行日志（journal）。

## 数据从哪来

归纳的**唯一依据**是：会话里已经能用于展示的消息与工具活动记录（含工具调用参数、工具返回结果，以及在流式阶段尚未落盘前、消息里可能存在的进行中工具片段）。

不会从以下来源推断：

- 助手自然语言里随口提到的文件名；
- Workspace 里存在但本会话从未通过工具触碰的文件；
- 其他字段的「猜测补全」。

## 待办（Tasks）

### 归纳规则

1. 在会话的工具结果中，查找 **`todo` 工具**返回的 JSON。
2. 只认顶层字段 **`todos[]`**（每项含 `id`、`content`、`status` 等）。
3. 若多轮交互里有多条 `todo` 结果，按 `id` 合并为当前任务列表快照；后续只含 `id/status` 的局部结果更新既有项，缺失 `content` 或返回 `(no description)` 时保留旧内容（不是历史变更流水）。

### 状态

常见状态：`pending`、`in_progress`、`completed`、`cancelled`；无法识别的非空状态可标为 `unknown`。

### 展示位置

- 右侧 Workspace Inspector 的 **Tasks** tab；
- Control Center 的 **Todos** 面板（优先用同一份 manifest；若尚未加载到 manifest，可临时从本地已加载的消息里找最新 `todos[]`）。

### 当前限制

- **流式过程中的滞后**：界面可能在工具卡上已看到 `todo` 完成，但 manifest 往往要等**本轮对话结束并持久化**后，才能稳定读到最新 `todos[]`（见下文「刷新与流式边界」）。

## 成果（Artifacts）

### 归纳规则

聚合统计当前会话所有轮次中由**写入类工具**带来的路径，例如：新建文件、编辑文件、应用 patch 等（含部分 MCP 文件系统写操作别名）。

路径来源包括：

- 工具参数里明确的路径字段；
- patch / diff 文本中能解析出的目标文件。

**不算**成果的情况：

- 仅 `read_file`、`grep`、`list_dir` 等只读操作（归入「参考」）；
- 助手回复里提到「我改了某某文件」但未产生对应工具记录；
- 整个 workspace 目录扫描结果。

### 路径与预览

- **Workspace 内**：路径以相对 workspace 的形式展示，可与现有「在工作区内打开/预览」能力衔接。
- **Workspace 外**：可列出绝对路径及是否存在、大小、修改时间等元数据，但**不**通过 workspace 预览能力打开任意系统路径。
- 常见依赖/构建目录（如 `.git`、`node_modules`、虚拟环境等）会被过滤，避免污染列表。

同一文件若在不同轮次或同一轮中被多次写入，列表中合并为一条，可附带多次命中的来源信息（来自哪次工具、大致在会话中的位置）。当前阶段不按轮次拆分展示成果。

## 参考（References）

### 归纳规则

聚合统计当前会话所有轮次中 Agent **实际读取/打开内容**的来源，例如读取文件、打开文件、查看文件等。

搜索、列目录、glob、rg/grep、语义搜索等只说明 Agent 发现了候选位置，不代表它已经阅读了命中文件内容。因此这些发现类操作**不默认进入 References**。

**不算**参考的情况：

- 写入类工具改过的文件（归入「成果」）；
- 搜索命中的文件，但后续没有被读取/打开；
- 列目录看到的目录或文件名；
- 助手正文里提到的路径，但没有对应读取记录。

### 路径与预览

- Workspace 内、且支持预览的文件：可从 **Refs** 跳转查看（通常先切到 Files 再打开）。
- Workspace 外路径：仅作「本会话读过哪些上下文」的线索，不提供任意路径预览。
- 同一内容来源若在多轮中被重复读取，References 中合并为一条。当前阶段不按轮次拆分展示参考。

## 界面如何消费 Manifest

用户侧可理解为：

1. 进入或切换某个会话时，拉取该会话的 manifest 并缓存。
2. 右侧面板在 **Files / Tasks / Artifacts / References** 四个 tab 间切换；后三个 tab 的数据来自 manifest。
3. Control Center 的 Todos 与右侧 Tasks 在「有 manifest 待办数据」时应保持一致口径。

当前阶段采用会话级聚合展示：

- Tasks 展示从 `todo` 工具结果合并出的最新快照；
- Artifacts 展示所有轮次产生的成果，按路径聚合去重；
- References 展示所有轮次实际读取/打开过的内容来源，按来源聚合去重；
- Turns 展示每轮 user-agent 交互里的成果与参考，聊天区可在对应轮次下方展示本轮成果文件。

当 manifest 暂时不可用或成果列表为空时，**成果** tab 可能用当前页面上已知的工具活动做**窄范围**的补充（仅写入类工具与 diff 片段），一旦 manifest 返回则以后端归纳结果为准。

## 刷新与流式边界

Manifest 会在这些时机**重新拉取**（通常带短防抖，避免工具密集时请求风暴）：

- 打开或切换会话；
- 流式对话中出现工具开始/工具完成类事件；
- 本轮对话结束（服务端已把完整会话写回）；
- 离开会话或清空当前会话上下文。

需要区分的两层状态：

| 阶段 | 用户可能看到 | Manifest 通常反映 |
| --- | --- | --- |
| **流式进行中** | 聊天区 live 工具卡、进行中 token | 多为**上一轮已持久化**的 Tasks / Artifacts / Refs |
| **本轮 `done` 之后** | 完整消息与工具结果落盘 | 与本回合工具活动对齐的最新 manifest |

因此常见现象：

- **成果**：流式时可能通过页面上的工具活动较早看到个别文件，manifest 稍后才对齐；
- **待办**：往往要等本轮结束后，Tasks / Todos 才稳定显示最新 `todos[]`。

若未来要「流式实时更新待办」，需要单独定义：是以 SSE 工具结果做乐观更新，还是让 manifest 能读取进行中的工具状态——这属于产品/状态层决策，而非单纯改展示文案。

## 实时增量事件（SSE）

流式阶段使用 `manifest_delta` SSE 事件做乐观更新。它是**运行时观察事件**，不是 transcript 消息：

- 不写入会话消息列表；
- 不进入模型上下文；
- 不替代 `/api/session/manifest`；
- 会通过 run journal 回放，因此页面刷新或 SSE 重连时需要幂等合并。

本轮 `done` 后，前端必须重新拉取 `/api/session/manifest`，并以持久化消息派生出的 manifest 覆盖所有 SSE 乐观结果。

### 事件结构

```json
{
  "version": 1,
  "session_id": "abc123",
  "stream_id": "stream-xyz",
  "turn_key": "live:stream-xyz",
  "sequence": 7,
  "source": {
    "kind": "tool_complete",
    "tool": "todo",
    "tid": "call-1",
    "status": "completed"
  },
  "todos": {
    "items": [
      {"id": "plan", "content": "Plan implementation", "status": "completed"}
    ],
    "mode": "replace_latest"
  },
  "artifacts": [
    {
      "path": "api/session_manifest.py",
      "kind": "file",
      "source_tool": "write_file",
      "status": "completed",
      "previewable": true
    }
  ],
  "references": [
    {
      "path": "docs/session-inspector-manifest.md",
      "kind": "file",
      "source_tool": "read_file",
      "status": "completed",
      "previewable": true
    }
  ]
}
```

字段说明：

- `version`：协议版本，初始为 `1`。
- `session_id` / `stream_id`：前端用来丢弃非当前会话或过期 stream 的事件。
- `turn_key`：事件归属的轮次。历史 manifest 使用 `turn:<user_msg_idx>`；实时阶段可先使用 `live:<stream_id>`，前端映射到当前 optimistic turn，`done` 后以后端 manifest 覆盖。
- `sequence`：同一 stream 内单调递增，用于前端和 run journal replay 幂等处理。
- `source`：说明事件来自工具开始还是完成；`tool` 和 `tid` 只用于展示来源与去重，不用于推断缺失字段。
- `todos`：只在明确解析到 `todo` 顶层 `todos[]` 时出现；`mode: replace_latest` 表示前端替换当前 Tasks 快照。流式阶段该快照由后端 live state 合成，后续只含 `id/status` 的局部工具结果会按 `id` 合并到既有列表。
- `artifacts`：只包含写入类工具或 diff/patch 中明确解析出的路径。
- `references`：只包含实际读取/打开内容的工具来源。

### 合并规则

- 顶层 Artifacts / References 按 `path` 聚合去重；轮次内按 `turn_key + path` 聚合去重。
- 对同一 `path` 的多次命中，保留最近 `status`，并可追加 `hits[]` 来源信息。
- `todos` 使用最新 `replace_latest` 快照，不做历史流水。实时阶段如果工具结果只包含部分 todo，后端按 `id` 合并；缺失 `content` 或返回 `(no description)` 时保留旧内容。
- 字段缺失时保持为空或跳过，不从相似字段自动补全。
- 前端收到重复 `sequence` 或重复 `turn_key + path + source.tid` 时必须幂等处理。

## 工具解析矩阵

工具解析是显式白名单，不按工具名相似性、字段名相似性或助手正文自动推断。

### Tasks

- 解析工具：`todo`。
- 解析时机：
  - `tool_start`：不解析 todos，因为工具尚未返回最终 `todos[]`。
  - `tool_complete`：解析工具结果中明确存在的顶层 `todos[]`，发送 `manifest_delta.todos`。
  - `/api/session/manifest` 构建：扫描持久化 `role='tool'` 消息内容中的顶层 `todos[]`，只取时间上最新的一条。
- 字段来源：
  - 只认工具结果 JSON 的顶层 `todos[]`。
  - 每个 todo 只取 `id`、`content`、`status`。
  - 未识别的非空 status 标为 `unknown`。
- 不解析 assistant prose 里的 todo 列表、markdown checklist、其它工具返回里碰巧含有相似字段的内容。

### Artifacts

- 解析工具：`write_file`、`create_file`、`edit_file`、`patch`、`apply_patch`、`mcp_filesystem_write_file`、`mcp_filesystem_edit_file`。
- 解析时机：
  - `tool_start`：仅从明确的工具参数中解析路径，生成 `status='in_progress'` 的 delta。
  - `tool_complete`：再次解析参数，并从工具结果或 patch/diff 文本中解析目标文件，生成完成或错误状态的 delta。
  - `/api/session/manifest` 构建：从持久化 assistant/tool 消息和 `session.tool_calls` 重建全会话 artifacts，并归属到对应 turn。
- 字段来源：
  - 工具参数里的明确路径字段，如 `path`、`file_path`、`target`、`destination`、`filename`、`paths[]`、`edits[].path`。
  - unified diff 中的 `+++ b/path` / `--- a/path`。
  - ApplyPatch 文本中的 `*** Add File:` / `*** Update File:`。
- 不解析只读工具结果、assistant 正文里说“我改了某文件”的 prose、全 workspace 扫描结果，以及 `.git`、`node_modules`、虚拟环境、构建目录等忽略路径。

### References

- 解析工具：`read_file`、`open_file`、`view_file`、`mcp_filesystem_read_file`。
- 可记录为目录参考的工具：`list_dir`、`mcp_filesystem_list_directory`，条目 `kind` 为 `dir`。
- 默认不进入 References 的发现类工具：`glob`、`rg`、`grep`、`search`、`semantic_search`、`mcp_filesystem_search_files`。
- 解析时机：
  - `tool_start`：只在参数中有明确读取目标时，可生成 `status='in_progress'` 的 reference delta。
  - `tool_complete`：确认工具完成后生成 `status='completed'` 的 reference delta；读文件工具只保留 path，不保存完整内容。
  - `/api/session/manifest` 构建：从持久化工具调用中重建 references。
- 不解析搜索命中的文件列表、列目录看到的每个子文件、assistant prose 中提到的路径。

### 解析时机总表

| 阶段 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不解析 | 从写入类工具参数解析明确路径，标记 `in_progress` | 从读取/打开工具参数解析明确路径，标记 `in_progress` |
| `tool_complete` | 从 `todo` 结果顶层 `todos[]` 解析最新快照 | 从写入类工具参数、结果、diff/patch 解析目标路径 | 从读取/打开工具参数确认实际引用来源 |
| `/api/session/manifest` | 从持久化 tool 消息按 `id` 合并 `todos[]` | 从持久化工具活动重建全会话和轮次 artifacts | 从持久化工具活动重建全会话和轮次 references |
| SSE replay | 重放已 journaled 的 `manifest_delta`，前端幂等合并 | 重放已 journaled 的 `manifest_delta`，前端幂等合并 | 重放已 journaled 的 `manifest_delta`，前端幂等合并 |

## 设计约束（实现时必须遵守）

1. **派生而非权威**：不替代 transcript；不以 manifest 驱动 Agent 执行。
2. **Artifacts ⊆ 全会话写入工具产物**：跨轮次聚合去重，不混入全 workspace、不混入只读访问。
3. **References ⊆ 全会话实际读取的内容来源**：跨轮次聚合去重，搜索命中、目录列表、助手 prose 提到的路径都不默认算参考。
4. **Tasks = 合并后的最新 `todo` 快照**：不是 todo 历史；局部更新按 `id` 合并。
5. **路径安全**：workspace 内相对路径 + 既有预览边界；workspace 外仅列表与元数据。
6. **字段诚实**：无明确来源则不跨字段推断、不自动复制相似字段填坑。
7. **SSE delta 是乐观派生状态**：只用于流式实时展示；本轮完成后以后端持久化 manifest 为准。

## 已知后续方向

- 扩展更多实际读取/写入工具别名时，需要同步更新本文件的工具解析矩阵和 contract tests。
- 若未来要展示每轮 References，也应继续使用 `turns[]`，不要从搜索命中或助手正文推断。
