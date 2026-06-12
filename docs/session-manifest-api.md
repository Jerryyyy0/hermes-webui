# Session Manifest 接口文档

本文档描述 **待办（Tasks）**、**成果（Artifacts）**、**参考（References）** 与 **轮次（Turns）** 的 HTTP/SSE 接口、字段契约与合并规则。产品语义与边界见 [session-inspector-manifest.md](./session-inspector-manifest.md)。

实现入口：`api/session_manifest.py`（构建与 delta）、`api/routes.py`（HTTP）、`api/streaming.py` / `api/gateway_chat.py`（SSE）、`static/workspace.js`（前端缓存）。

---

## 1. 概览

**Session Manifest** 是会话工具活动的**派生索引**，不替代 transcript，也不扫描整个 workspace。

| 字段 | 含义 | 唯一数据来源 |
| --- | --- | --- |
| `todos` | 待办最新快照 | `todo` 工具结果 JSON 顶层 `todos[]` |
| `artifacts` | 写入类工具创建/修改过的路径，及明确交付的文件 | 写入工具白名单 + diff/patch + `MEDIA:` + turn reconcile（assistant 交付 prose / 工具交付输出） |
| `references` | 实际读取/打开的内容来源 | 读取/列目录工具白名单 |
| `turns[]` | 按 user 消息划分的轮次视图 | 同上，归属 `turn:<user_msg_idx>` |

**明确不算入**：助手正文**普通提及**的路径（无明确交付关键词）、搜索命中但未读取的文件、目录列表中的子文件名、跨字段推断补全。助手**明确交付语句**（如 `文件路径：...`、`已保存: ...`）且 workspace 内文件真实存在时，进入 `artifacts[]`，不进入 `references[]`。

---

## 2. HTTP：获取持久化 Manifest

### `GET /api/session/manifest`

#### 请求

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | query | 是 | 会话 ID |

示例：

```http
GET /api/session/manifest?session_id=abc123
```

#### 成功响应 `200`

```json
{
  "manifest": {
    "todos": {
      "items": [
        { "id": "1", "content": "Research", "status": "in_progress" }
      ]
    },
    "artifacts": [
      {
        "path": "api/session_manifest.py",
        "preview": "file",
        "source_tool": "write_file"
      }
    ],
    "references": [
      {
        "path": "my-skill",
        "preview": "skill",
        "source_tool": "skill_view"
      }
    ],
    "turns": [
      {
        "turn_key": "turn:0",
        "artifacts": [],
        "references": []
      }
    ]
  },
  "live": {
    "stream_id": "stream-xyz",
    "source": "active_stream"
  }
}
```

`live` 仅在有活跃流式 manifest 时出现；否则为 `{ "manifest": { ... } }`。

**不包含**：`session_id`、`workspace`（请求参数 / session 对象已有）、`counts`（前端用数组长度）。

#### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `session_id` |
| `404` | 会话不存在 |
| `500` | 构建 manifest 失败 |

#### 服务端构建流程（权威态）

1. 加载会话展示用消息 + `session.tool_calls`，收集 `ToolEvent`。
2. **Todos**：仅在**当前轮**（最后一个 `role=user` 之后）的 `role=tool` 消息中解析顶层 `todos[]`，按 `id` 合并；同轮内 `content` 为空或为 `(no description)` 时保留旧 `content`。对外下发的 `todos.items` 只包含带可展示 `content` 的条目（无则 `items: []`）。
3. **Artifacts / References**：按工具白名单与路径规则归纳，会话级按 `path` 去重。
4. **Turns**：每个 `role=user` 开启一轮，`turn_key = turn:<user_msg_idx>`。
5. 过滤并序列化为可预览行（仅 `path`、`preview`、`source_tool`）。

#### 前端拉取时机

- 打开或切换会话；
- 本轮对话 `done` 后（`scheduleRefreshSessionManifest`，约 120ms 防抖）；
- 离开会话时清空缓存（`clearSessionManifest`）。

本轮 `done` 后必须以本接口结果**覆盖**所有 SSE 乐观合并结果。

---

## 3. SSE：流式增量 `manifest_delta`

与聊天流共用 SSE 连接；事件名：`manifest_delta`（`static/messages.js` 订阅，经 `HermesSessionInspector.applyDelta` 合并）。

### 3.1 载荷结构（`version: 1`）

```json
{
  "version": 1,
  "session_id": "abc123",
  "stream_id": "stream-xyz",
  "turn_key": "turn:42",
  "sequence": 7,
  "source": {
    "kind": "tool_complete",
    "tool": "write_file",
    "tid": "call-1",
    "status": "completed"
  },
  "todos": {
    "items": [
      { "id": "plan", "content": "Plan implementation", "status": "completed" }
    ],
    "mode": "replace_latest"
  },
  "artifacts": [
    {
      "path": "api/session_manifest.py",
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": []
}
```

SSE 行级形状与 GET manifest **相同**（三字段）。`todos.mode` 仅出现在 SSE delta，不出现在 GET `manifest.todos`。

### 3.2 顶层字段

| 字段 | 说明 |
| --- | --- |
| `version` | 协议版本，当前为 `1` |
| `session_id` | 前端丢弃非当前会话事件 |
| `stream_id` | 配合 `sequence` 做幂等与过期流过滤 |
| `turn_key` | 后端在 stream 启动时确定，格式 `turn:<user_msg_idx>`；前端不得从 `stream_id` 推断 |
| `sequence` | 单 stream 内单调递增 |
| `source` | `tool_start` 或 `tool_complete`；`tool`、`tid` 仅用于展示与去重 |
| `todos` | 可选；仅 `todo` 完成时；服务端发出前常与 live manifest 合并 |
| `artifacts` / `references` | 可选；本次工具事件解析出的增量行 |

若 `todos`、`artifacts`、`references` 均为空，服务端**不发送**该事件。

### 3.3 发射时机

| 阶段 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不发射 | 写入工具参数路径 → `status: in_progress` | 读取工具参数路径 → `status: in_progress` |
| `tool_complete` | 解析结果顶层 `todos[]` | 参数 + 结果 + diff/patch → `completed` / `error` | 参数确认 → `completed` |
| `turn_complete`（`done` 前） | 不发射 | 本轮 transcript reconcile（见 §4.2）→ `source.tool: reconcile` | 不发射 |

### 3.4 与 HTTP 的关系

- SSE 为**乐观派生状态**，不写入 transcript，不进入模型上下文。
- 不替代 `GET /api/session/manifest`。
- 可通过 run journal 回放；前端须幂等合并。
- **仅更新侧栏 Inspector**；不触发聊天区 per-turn 成果 chips。

---

## 4. 三类数据：字段契约

### 4.1 待办 `manifest.todos`

**解析工具**：仅 `todo`。

**容器字段（GET manifest）**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `items` | `object[]` | 当前任务列表快照 |

SSE delta 的 `todos` 可额外含 `mode: "replace_latest"`（合并语义，不下发到 GET）。

**`items[]` 每项**

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `id` | 工具结果 | 必填；合并键 |
| `content` | 工具结果 | 空或 `(no description)` 时保留旧值 |
| `status` | 工具结果 | `pending` \| `in_progress` \| `completed` \| `cancelled`；其它非空 → `unknown` |

**语义**：最新快照，非历史流水。多轮 `todo` 按 `id` 合并。

**展示**：Workspace **Tasks** tab；Control Center **Todos**（优先 manifest）。

---

### 4.2 成果 `manifest.artifacts[]`

#### 写入工具白名单

**Workspace 文件**：`write_file`, `create_file`, `edit_file`, `patch`, `apply_patch`, `mcp_filesystem_write_file`, `mcp_filesystem_edit_file`

**Skill 成果**：

1. **Hermes Agent `skill_manager_tool`**：`skill_manage`，且 `action` 为 `create` / `edit` / `patch` / `write_file` — 从 args 的 `name` 取技能名；`tool_complete` 后若 result JSON 含 `success` 与 `path`，优先用 `path`（如 `github/github-trending`）。
2. **通用文件写入工具**：上述 workspace 写入类工具（如 `write_file`、`edit_file`）直接写入 session profile 的 `{HERMES_HOME}/skills/.../SKILL.md` 时，manifest `path` 为相对 skills 根的技能名（如 `my-skill` 或 `github/github-trending`）。

两种来源均为 `preview: "skill"`。须 `HERMES_INTEGRATION=1`、SkillHub 可用，且 profile skills 目录下已存在对应 `SKILL.md`（`in_progress` 不出现在 wire）。skills 目录下非 `SKILL.md` 文件不算 skill 成果。

Skill 成果示例：

```json
{
  "path": "deep-research-zh",
  "preview": "skill",
  "source_tool": "skill_manage"
}
```

#### 路径来源

- 参数：`path`, `file_path`, `target`, `destination`, `filename`, `paths[]`, `edits[].path` 等；
- Unified diff：`+++ b/path` / `--- a/path`；
- ApplyPatch：`*** Add File:` / `*** Update File:`；
- assistant 正文：`MEDIA:<local-path>`（`source_tool: "media"`；仅 `role=assistant`；跳过 `MEDIA:https://...`）；
- **turn_complete reconcile**（`done` 前 SSE + `GET` 重建）：从本轮 tool args/result/diff、`MEDIA:` 与 **assistant 交付 prose**（如 `文件位置：` 后的路径）保守正则提取候选路径；**须通过 `_file_preview_path`（workspace 内真实存在、可预览）** 才可 wire；`source.kind: turn_complete`、`source.tool: reconcile`；prose 行 `source_tool: assistant_prose`；各行 `source_tool` 仍为实际工具名或 `media`/`assistant_prose`。

#### 单条结构（与 references 共用）

```json
{
  "path": "api/session_manifest.py",
  "preview": "file",
  "source_tool": "write_file",
  "profile": "ops"
}
```

`profile`（可选）：成果所属 WebUI profile，来自 `session.profile`；无明确值时省略。仅 `artifacts[]` 携带，不在 `references[]` 中。

**只返回可预览项**。不可预览路径（目录、缺失、过大、cruft 等）不出现在列表中。预览接口与路由见 [§4.5 预览逻辑](#45-预览逻辑file--skill)。

#### 排除

只读工具、无 `MEDIA:` 标记的助手 prose、`role=user` 中的 `MEDIA:`、远程 `MEDIA:` URL、工具 JSON 的 `file_path`/`media_tag`、全 workspace 扫描、workspace 外写入类工具路径（workspace 外仅 `source_tool=media` 可列出）。

#### SSE `turn_complete`（reconcile）

assistant 消息持久化后、`done` 前可发送 `manifest_delta`：`source.kind = "turn_complete"`，`source.tool = "reconcile"`。载荷含本轮 transcript 补全成果（含 `MEDIA:`、assistant 交付 prose、非白名单写入工具的 args/result/diff 路径）；**候选路径须 workspace 内真实存在且可预览**，文本命中但文件不存在一律丢弃。prose 路径使用 `source_tool: assistant_prose`。仅更新侧栏 Inspector；per-turn chips 仍以 turn `done` 后的 `GET /api/session/manifest` 为准。

#### 展示

- 右侧 **Artifacts** tab：全会话按 `path` 聚合；
- 聊天区：每轮 `done` 后展示 `turns[].artifacts` chips。

---

### 4.3 参考 `manifest.references[]`

#### 读取工具白名单

`read_file`, `open_file`, `view_file`, `mcp_filesystem_read_file`, `skill_view`

#### 发现类工具（默认不进 References）

`glob`, `rg`, `grep`, `search`, `semantic_search`, `mcp_filesystem_search_files`, `list_dir`, `mcp_filesystem_list_directory`（目录不可预览，过滤）

#### 单条结构

与 artifacts **相同三字段**。读文件工具**不保存**文件正文。`skill_view` 条目 `preview` 为 `"skill"`，预览见 [§4.5](#45-预览逻辑file--skill)。

#### 排除

已写入路径（归 artifacts）、搜索命中未读、list 目录、助手提到的路径、workspace 外路径。

#### 展示

右侧 **Refs** tab；聊天区当前不展示 per-turn references。

---

### 4.4 轮次 `manifest.turns[]`

```json
{
  "turn_key": "turn:0",
  "artifacts": [
    {
      "path": "notes.txt",
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": []
}
```

| 字段 | 说明 |
| --- | --- |
| `turn_key` | `turn:<user_msg_idx>`，与 SSE、聊天 `data-turn-key` 一致 |
| `artifacts` / `references` | 本轮可预览子集，元素形状同上 |

聊天区 per-turn 成果仅使用 `turns[].artifacts`。

---

### 4.5 预览逻辑（file / skill）

Manifest **只负责列出可预览项并给出路由标识** `preview`；**不返回文件/技能正文**。用户点击 Artifacts、References 或聊天区 turn chips 时，前端 `HermesSessionInspector.openManifestPreview(item)` 仅根据 `item.preview` 选择下方两条路径之一。

#### 端到端流程

```mermaid
flowchart TD
  click[用户点击 manifest 行]
  click --> check{item.preview}
  check -->|file| pathShape{path 形态}
  pathShape -->|相对路径| fileApi[integration workspace file API]
  pathShape -->|绝对路径| mediaApi["/api/media + session_id"]
  check -->|skill| skillApi[skillhub content API]
  fileApi --> panel[右侧 Workspace 预览区]
  mediaApi --> panel
  skillApi --> panel
```

| 步骤 | 说明 |
| --- | --- |
| 1. 服务端构建 | `api/session_manifest.py` 从工具事件归纳路径；**仅可预览**者进入 `artifacts[]` / `references[]`，并写入 `preview: "file"` 或 `"skill"` |
| 2. 前端展示 | 侧栏 / turn chips 渲染 `path` + `source_tool`；列表内条目均可点击（无 `previewable` 字段） |
| 3. 用户点击 | `openManifestPreview({ path, preview, source_tool })` |
| 4. 拉取内容 | `preview === "file"` 且 `path` 为相对路径 → integration 文件接口；`path` 为绝对路径（`source_tool=media`）→ `/api/media?path=&session_id=`；`preview === "skill"` → SkillHub 正文接口 |
| 5. 渲染 | 同一套 Workspace 预览区（`previewArea`），按扩展名或 Markdown 选择 code / md / pdf / html / 媒体等模式 |

**Manifest 预览不走** session 级 `GET /api/file?session_id=`，也**不走**已移除的 `GET /api/file/allowlisted`。右侧文件树手动浏览仍可用 session `/api/file`（与 manifest 预览相互独立）。

#### 部署前提（file 预览）

Integration 文件接口根目录为 **`HERMES_WEBUI_DEFAULT_WORKSPACE`**，**无 `session_id` 参数**。因此：

- 仅当 **session workspace 与 `DEFAULT_WORKSPACE` 一致** 时，manifest 中的 workspace 相对 `path` 与 integration 读盘根目录一致；
- 若两者不一致，manifest 可能仍列出 session workspace 内文件，但点击预览会读到错误路径或 404。

#### 服务端：何时写入 `preview`

| `preview` | 写入条件（全部满足才出现在 manifest） |
| --- | --- |
| `"file"` | 写入/读取/MEDIA 命中路径；workspace 内相对路径 **或** `source_tool=media` 的 workspace 外绝对路径；目标为**文件**、存在、非 cruft、未超 `MAX_FILE_BYTES` |
| `"skill"`（References） | 工具为 `skill_view`；`HERMES_INTEGRATION=1` 且 SkillHub 可用；技能名（写入 `path`）非空 |
| `"skill"`（Artifacts） | （1）`skill_manage` 且 `action` 为写入类；或（2）写入类工具路径为 profile `{HERMES_HOME}/skills/.../SKILL.md`；integration 可用；对应 `SKILL.md` 存在；非 `in_progress` |

**不写入、不出现在列表**：目录、`list_dir` 路径、缺失文件、非 MEDIA 的 workspace 外路径、profile 记忆文件（`MEMORY.md` 等）、过大文件、SkillHub 不可用时的 `skill_view`、远程 `MEDIA:` URL。

#### `preview: "file"` — workspace 文件

**接口**（需 `HERMES_INTEGRATION=1`）：

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| `GET` | `/api/integration/workspace/file` | `path` = manifest 行的 workspace **相对路径**（POSIX） |

**成功 `200`**：原始文件字节；`Content-Type` 按扩展名；`Cache-Control: no-store`；**不设** `Content-Disposition`。支持 `Range` 字节范围。

**错误**：`{ "error": "<message>" }` — `400` 缺 `path`；`404` 不存在 / 非文件 / cruft。

**前端按扩展名渲染**（`static/workspace.js` → `openIntegrationFilePreview`）：统一 `GET /file?path=`；文本/md/html 用 `fetch` → `text()`；媒体/PDF 可用直链 URL；Office/压缩包等 `fetch` blob 后 `<a download>`；HTML 用 sandbox `srcdoc`。

**示例**：

```http
GET /api/integration/workspace/file?path=docs/readme.md
GET /api/integration/workspace/file?path=assets/logo.png
```

实现入口：[`integration/workspace/handlers.py`](../integration/workspace/handlers.py)；与左侧 integration 文件栏共用同一 API。

#### `preview: "skill"` — 技能正文

**来源**：仅 **`skill_view`** 工具；manifest 行 `path` = **技能名**（不是磁盘路径），`source_tool` = `skill_view`。

**接口**（需 SkillHub / integration 技能模块可用）：

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| `GET` | `/api/skillhub/content` | `name` = manifest 行的 `path`（技能名）；可选 `scope`（默认 **`auto`**：本地 `{HERMES_HOME}/skills` 优先，否则 SkillHub `/doc`；`custom` / `hub` 强制仅本地 / 仅上游） |

**成功 `200`**：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 技能名 |
| `content` | string | 是 | SKILL.md 全文 |
| `linked_files` | object | 否 | hub 常为 `{}` |

```json
{
  "name": "my-skill",
  "content": "---\nname: my-skill\n---\n\n# Skill body...",
  "linked_files": {}
}
```

**错误**：`400` 缺 `name`；`404`（`scope=custom` 或 `auto` 且本地与上游均不可用）；`502` hub 上游失败。体为 `{ "error": "..." }`。

**前端行为**（`openSkillContentPreview`）：

1. `GET /api/skillhub/content?name={path}`
2. 标题栏显示 `data.name` 或技能名
3. 正文按 Markdown 渲染（与 `.md` 文件预览相同逻辑）；过大则纯文本 code 模式

**Manifest 预览不包含**：`/api/skillhub/structure`（目录树）、`/api/skillhub/file`（技能包内单文件）。当前产品只预览 **SKILL 主文档正文**。

#### 前端分发（契约）

```javascript
// static/workspace.js — 逻辑摘要
async function openManifestPreview(item) {
  if (!item?.path || !item.preview) return;
  if (item.preview === 'skill') return openSkillContentPreview(item.path);
  if (item.preview === 'file') return openIntegrationFilePreview(item.path);
}

function isManifestPreviewable(item) {
  return item?.preview === 'file' || item?.preview === 'skill';
}
```

| manifest 字段 | file 预览 | skill 预览 |
| --- | --- | --- |
| `path` | 传给 `?path=` / raw 的相对路径 | 传给 `?name=` 的技能名 |
| `preview` | 必须为 `"file"` | 必须为 `"skill"` |
| `source_tool` | 仅展示（如 `write_file`、`read_file`） | 通常为 `skill_view` |

#### 与 SSE delta 的关系

流式 `manifest_delta` 中 `artifacts[]` / `references[]` **行形状与 GET 相同**（三字段）。若某次工具事件尚不可预览（例如 `write_file` 进行中文件尚未落盘），该路径**不会**出现在 delta 中；本轮 `done` 后 `GET /api/session/manifest` 权威重建列表。

---

## 5. 工具解析矩阵

| 类型 | 工具 | `tool_start` | `tool_complete` | `GET /api/session/manifest` |
| --- | --- | --- | --- | --- |
| Tasks | `todo` | 不解析 | 顶层 `todos[]` → SSE（可展示 content） | 当前轮 tool 消息，按 `id` 合并 |
| Artifacts | 写入白名单 | 参数路径（可预览则入列表） | 参数+结果+diff | 持久化消息重建，过滤不可预览 |
| References | 读取+列目录白名单 | 参数路径（可预览则入列表） | 确认完成 | 持久化消息重建，过滤不可预览 |
| 排除 | 发现类搜索 | — | 不进 References | — |

扩展工具别名时须同步更新 [session-inspector-manifest.md](./session-inspector-manifest.md) 矩阵与 `tests/test_session_manifest*.py`。

---

## 6. 合并与幂等

| 对象 | 规则 |
| --- | --- |
| `artifacts` / `references`（会话级） | 按 `path` 去重；同 path 后者覆盖 `source_tool` |
| `turns[].artifacts` / `turns[].references` | 按 `turn_key` 合并；轮次内按 `path` 去重 |
| `todos` | 仅当前轮；按 `id` 合并；live 可含 id-only；SSE/GET 出站前过滤为可展示 `content` |
| 缺失字段 | 保持空或跳过，不跨字段推断 |
| 幂等 | 重复 `stream_id:sequence` 忽略 |

---

## 7. 生命周期

| 阶段 | 侧栏 Tasks / Artifacts / Refs | 聊天区 turn 成果 chips |
| --- | --- | --- |
| 流式进行中 | `manifest_delta` 乐观更新 | 不展示 |
| 本轮 `done` 后 | `GET /api/session/manifest` 覆盖 SSE | `refreshTurnArtifactsInChat()` |

---

## 8. 前端 API（浏览器）

全局对象 `window.HermesSessionInspector`（`static/workspace.js`）：

| 方法 | 说明 |
| --- | --- |
| `refresh()` | 调用 `loadSessionManifest()` |
| `clear()` | 清空缓存 |
| `applyDelta(delta)` | 合并 SSE `manifest_delta` |
| `openManifestPreview(item)` | 按 `preview`：`file` → integration 文件 API；`skill` → skillhub content（见 [§4.5](#45-预览逻辑file--skill)） |
| `isManifestPreviewable(item)` | `preview === 'file' \|\| preview === 'skill'` |

内部缓存：`_sessionManifest`、`_sessionManifestSid`；切换会话后 sid 不匹配则视为无 manifest。

---

## 9. 设计约束（实现须遵守）

1. **派生而非权威**：不替代 transcript；不以 manifest 驱动 Agent。
2. **Artifacts**：仅写入工具产物；跨轮聚合；不含只读访问。
3. **References**：仅实际读取/打开来源；搜索命中、目录子项、prose 路径默认不算。
4. **Tasks**：仅当前轮 `todo` 快照；同轮局部更新按 `id`；出站须有可展示 `content`。
5. **路径安全**：仅 workspace 内可预览文件进入列表；外路径过滤。
6. **字段诚实**：无明确来源则不推断、不复制相似字段。
7. **SSE 为乐观态**：本轮完成后以 `GET /api/session/manifest` 为准。

---

## 10. 相关测试

- `tests/test_session_manifest.py` — 构建、合并、HTTP 路由
- `tests/test_session_manifest_contract.py` — 路由与前端监听契约

本地验证示例（隔离状态目录见 `AGENTS.md`）：

```bash
python3 -m pytest tests/test_session_manifest.py tests/test_session_manifest_contract.py -q
```
