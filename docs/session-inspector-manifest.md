# 会话 Inspector Manifest：待办、成果与参考

本文档说明 WebUI 在**一次会话的多轮交互过程中**，如何从 Agent 的工具活动里归纳出右侧面板的三类信息，以及这些产品语义上的边界与限制。Artifacts / References 的**文件预览 HTTP 接口**见 [文件预览接口](#文件预览接口)；Manifest 拉取、SSE 与错误码等契约见 [session-manifest-api.md](./session-manifest-api.md)。

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

聚合统计当前会话所有轮次中由**写入类工具**带来的路径，以及 assistant 正文中显式交付的 **`MEDIA:` 本地文件**。

路径来源包括：

- 工具参数里明确的路径字段；
- patch / diff 文本中能解析出的目标文件；
- **`role=assistant` 消息**中的 `MEDIA:<local-path>`（正则与 `/api/media` 一致；`source_tool` 固定为 `media`）。

**不算**成果的情况：

- 仅 `read_file`、`grep`、`list_dir` 等只读操作（归入「参考」）；
- 助手回复里提到「我改了某某文件」但未产生对应工具记录；
- `role=user` 消息中的 `MEDIA:`、助手 prose 里随口提到的路径（无 `MEDIA:` 标记）；
- `MEDIA:https://...` 远程 URL、工具 JSON 里的 `file_path`/`media_tag`（未写入 assistant 正文）；
- 整个 workspace 目录扫描结果。

同一路径若既有写入类工具又有 `MEDIA:`，`source_tool` **保留写入工具名**（`write_file` 优先于 `media`）。

### 路径与预览

Manifest **只返回可预览条目**；每条仅含 `path`、`preview`（`"file"` | `"skill"`）、`source_tool` 三字段。不可预览路径（目录、缺失、过大、cruft 等）不出现在列表中；常见依赖/构建目录（如 `.git`、`node_modules`、虚拟环境等）会被过滤。用户点击条目时如何拉取正文，见下文 [文件预览接口](#文件预览接口)。

`preview: "file"` 时 **`path` 形态决定拉取接口**（不新增 preview 枚举）：

| `path` | 含义 | 预览拉取 |
| --- | --- | --- |
| workspace **相对路径** | session workspace 内文件 | `/api/integration/workspace/file?path=` |
| **绝对路径** | workspace 外、由 assistant `MEDIA:` 引用的本地文件（`source_tool=media`） | `/api/media?path=&session_id=` |

同一文件若在不同轮次或同一轮中被多次写入，**右侧 Artifacts tab** 中合并为一条。聊天区则在每轮对话 **done** 后，于该轮 assistant 下方展示本轮成果文件 chips，不在流式中途展示。

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

与 Artifacts 共用三字段行形状（`path`、`preview`、`source_tool`），仅包含可预览项；预览接口与 Artifacts 相同，见 [文件预览接口](#文件预览接口)。目录、workspace 外路径、未实际读取的内容不出现在 References 列表。同一内容来源若在多轮中被重复读取，**右侧 References tab** 中合并为一条；聊天区当前不展示 per-turn References。

## 文件预览接口

Manifest **只列出可预览项并标注** `preview`；**不内嵌文件或技能正文**。用户在右侧 **Artifacts / References** tab 或聊天区 per-turn 成果 chips 上点击某行时，前端根据 `item.preview` 调用对应 HTTP 接口，在右侧 Workspace **预览区**（`previewArea`）渲染。

完整请求/响应字段与错误码见 [session-manifest-api.md §4.5](./session-manifest-api.md#45-预览逻辑file--skill)。

### 端到端流程

1. 服务端从工具活动归纳路径，仅**可预览**者进入 `artifacts[]` / `references[]`，并写入 `preview: "file"` 或 `"skill"`。
2. 侧栏或 turn chips 展示 `path` 与 `source_tool`；凡出现在列表中的条目均可点击。
3. 点击后：`preview === "file"` → integration workspace 文件 API；`preview === "skill"` → SkillHub 技能正文 API。
4. 按扩展名或 Markdown 选择 code / markdown / pdf / html / 图片 / 音视频等预览模式。

**Manifest 预览不走** 带 `session_id` 的 `GET /api/file`（会话文件树手动浏览仍可用该路径，与 manifest 预览相互独立）。已移除的 `GET /api/file/allowlisted` 不再使用。

### 何时写入 `preview`

| `preview` | 出现在列表的条件（须全部满足） |
| --- | --- |
| `"file"` | 写入/读取工具命中路径；路径在 **session workspace 内**；目标为**文件**、存在、非 cruft、未超过大小上限 |
| `"skill"`（Refs） | 工具为 `skill_view`；integration / SkillHub 可用；`path` 为技能名（非磁盘路径） |
| `"skill"`（Artifacts） | （1）`skill_manage` 且 `action` 为写入类；或（2）写入类工具路径为 profile skills 目录下 `SKILL.md`；integration 可用；`SKILL.md` 存在 |

**不出现在列表**：目录、`list_dir` 路径、缺失文件、workspace 外路径、profile 记忆文件（`MEMORY.md` 等）、过大文件、SkillHub 不可用时的 `skill_view`。

### `preview: "file"` — workspace 文件

需启用 integration（`HERMES_INTEGRATION=1`）。读盘根目录为 **`HERMES_WEBUI_DEFAULT_WORKSPACE`**，接口**无** `session_id` 参数。

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| `GET` | `/api/integration/workspace/file` | `path` = manifest 行的 workspace **相对路径**（POSIX） |

示例：

```http
GET /api/integration/workspace/file?path=docs/readme.md
GET /api/integration/workspace/file?path=assets/logo.png
```

#### `GET /api/integration/workspace/file` — 返回值

**成功 `200`**：响应体为**原始文件字节**（非 JSON）。

| 响应头 | 说明 |
| --- | --- |
| `Content-Type` | 按扩展名映射（如 `text/plain` 未单独映射时多为 `application/octet-stream`；`.png` → `image/png`，`.pdf` → `application/pdf`） |
| `Cache-Control` | `no-store` |
| `Accept-Ranges` | `bytes`（支持 Range 请求） |

**不设置** `Content-Disposition`；内嵌预览与触发下载均由前端按扩展名处理（`fetch` + blob / 直链 URL / `<a download>`）。

**错误**（JSON `{ "error": "<message>" }`，路径类错误消息会脱敏）：

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `path` 或路径非法 |
| `404` | 文件不存在、非普通文件、或为 cruft（如 `.DS_Store`） |

**部署注意**：仅当 **session workspace 与 `HERMES_WEBUI_DEFAULT_WORKSPACE` 一致** 时，manifest 中的相对 `path` 与 integration 读盘根目录一致；否则列表可能仍有条目，但点击预览会 404 或读到错误文件。

**前端按扩展名处理**（摘要，`static/workspace.js` → `openIntegrationFilePreview`）：

| 类型 | 方式 | 预览 |
| --- | --- | --- |
| `.md` / `.markdown` / `.mdown` | `fetch` → `text()` | Markdown（过大则纯文本 code） |
| `.html` / `.htm` | `fetch` → `text()` → sandbox `iframe.srcdoc` | html |
| `.pdf`、图片、音视频 | 直链 URL 或 `fetch` blob | iframe / `<img>` / 媒体控件 |
| 其它文本 | `fetch` → `text()` | 语法高亮 code |
| Office / 压缩包等（`DOWNLOAD_EXTS`） | `fetch` blob → `<a download>` | 浏览器下载，不内嵌 |

与左侧 integration 文件栏共用上述 API（实现：`integration/workspace/handlers.py`）。

### `preview: "skill"` — 技能正文

来源仅为 **`skill_view`** 工具：manifest 行 `path` = **技能名**，`source_tool` = `skill_view`。

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| `GET` | `/api/skillhub/content` | `name` = manifest 行的 `path`（技能名） |

SkillHub UI 可选 `scope=custom`（仅本地）或 `scope=hub`（仅上游）；**manifest 预览不传 `scope` 时默认为 `auto`**（先在 `{HERMES_HOME}/skills` 解析，未找到再请求 SkillHub）。

#### `GET /api/skillhub/content` — 返回值

`Content-Type: application/json; charset=utf-8`。

**成功 `200`**（hub 与 custom 成功时形状一致；custom 由本地 `SKILL.md` 读出）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 技能名；上游缺省时回退为查询参数 `name` |
| `content` | string | 是 | SKILL 主文档全文（通常为带 frontmatter 的 Markdown） |
| `linked_files` | object | 否 | hub 代理固定为 `{}`；预留关联文件元数据，manifest 预览不使用 |

```json
{
  "name": "my-skill",
  "content": "---\nname: my-skill\n---\n\n# Skill body",
  "linked_files": {}
}
```

上游若返回 JSON 且正文在 `readme` 等字段，服务端会归一化为 `content`。非 JSON 响应时以响应体文本填入 `content`。

**前端读取**：`data.content || data.body || ''`（`body` 为兼容字段，hub 路径通常只有 `content`）；标题栏用 `data.name || name`。

**错误**（JSON `{ "error": "<message>" }`）：

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `name` |
| `404` | `scope=custom` 或 `auto` 且本地与上游均不可用 |
| `502` | hub 上游请求失败（异常信息写入 `error`） |

正文按 Markdown 渲染（与 `.md` 文件预览相同；过大则纯文本 code）。

**Manifest 预览不包含**：`/api/skillhub/structure`（目录树）、`/api/skillhub/file`（技能包内单文件）。

### 字段与 manifest 行的对应关系

| manifest 字段 | `preview: "file"` | `preview: "skill"` |
| --- | --- | --- |
| `path` | 传给 `?path=` / raw 的 workspace 相对路径 | 传给 `?name=` 的技能名 |
| `preview` | 必须为 `"file"` | 必须为 `"skill"` |
| `source_tool` | 展示来源（如 `write_file`、`read_file`） | 通常为 `skill_view` |

流式 `manifest_delta` 中 `artifacts[]` / `references[]` **行形状与 GET manifest 相同**。工具进行中文件尚未落盘等情况下，该路径可能暂不出现在 delta；本轮 **done** 后以 `GET /api/session/manifest` 权威列表为准。

## 界面如何消费 Manifest

用户侧可理解为：

1. 进入或切换某个会话时，拉取该会话的 manifest 并缓存。
2. 右侧面板在 **Files / Tasks / Artifacts / References** 四个 tab 间切换；后三个 tab 的数据来自 manifest。
3. Control Center 的 Todos 与右侧 Tasks 在「有 manifest 待办数据」时应保持一致口径。

当前阶段采用会话级聚合展示：

- Tasks 展示从 `todo` 工具结果合并出的最新快照；
- Artifacts 展示所有轮次产生的成果，按路径聚合去重（右侧 tab）；
- References 展示所有轮次实际读取/打开过的内容来源，按来源聚合去重（右侧 tab）；
- Turns 供聊天区 per-turn 成果展示：每轮 **done** 后，在该轮 assistant 下方展示本轮 `artifacts[]`（仅写入类工具明确解析出的路径）。

当 manifest 暂时不可用或成果列表为空时，**成果** tab 可能用当前页面上已知的工具活动做**窄范围**的补充（仅写入类工具与 diff 片段），一旦 manifest 返回则以后端归纳结果为准。

## 刷新与流式边界

Manifest 会在这些时机**重新拉取**（通常带短防抖，避免工具密集时请求风暴）：

- 打开或切换会话；
- 本轮对话结束（服务端已把完整会话写回）；
- 离开会话或清空当前会话上下文。

流式过程中，`manifest_delta` SSE 仅更新侧栏 Inspector 缓存，不触发聊天区 per-turn 成果渲染。

需要区分的两层状态：

| 阶段 | 用户可能看到 | Manifest 通常反映 |
| --- | --- | --- |
| **流式进行中** | 聊天区 live 工具卡、进行中 token；侧栏 Tasks / Artifacts / Refs 可走 SSE 乐观更新 | 侧栏 manifest 缓存经 `manifest_delta` 增量合并；**聊天区 per-turn 成果 chips 不展示** |
| **本轮 `done` 之后** | 完整消息与工具结果落盘；聊天区各轮 assistant 下方出现本轮成果 chips | 与本回合工具活动对齐的最新 manifest（`/api/session/manifest` 覆盖 SSE 乐观结果） |

因此常见现象：

- **侧栏成果**：流式时可通过 `manifest_delta` 较早看到个别文件；`done` 后以持久化 manifest 为准。
- **聊天区 per-turn 成果**：只在 turn **done** 且 manifest 拉取完成后展示；流式中途不出现。
- **待办**：侧栏 Tasks / Todos 可在流式中经 `manifest_delta` 更新；`done` 后以持久化 manifest 为准。

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
  "turn_key": "turn:42",
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
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": [
    {
      "path": "docs/session-inspector-manifest.md",
      "preview": "file",
      "source_tool": "read_file"
    }
  ]
}
```

字段说明：

- `version`：协议版本，初始为 `1`。
- `session_id` / `stream_id`：前端用来丢弃非当前会话或过期 stream 的事件。
- `turn_key`：事件归属的轮次，统一使用 `turn:<user_msg_idx>`。该值由后端在 stream 启动时确定，实时 SSE 和历史 manifest 使用同一个 key；前端不从 `stream_id` 推断轮次。
- `sequence`：同一 stream 内单调递增，用于前端和 run journal replay 幂等处理。
- `source`：说明事件来自工具开始还是完成；`tool` 和 `tid` 只用于展示来源与去重，不用于推断缺失字段。
- `todos`：只在明确解析到 `todo` 顶层 `todos[]` 时出现；`mode: replace_latest` 表示前端替换当前 Tasks 快照。流式阶段该快照由后端 live state 合成，后续只含 `id/status` 的局部工具结果会按 `id` 合并到既有列表。
- `artifacts`：只包含写入类工具或 diff/patch 中明确解析出的路径。
- `references`：只包含实际读取/打开内容的工具来源。

### 合并规则

- 顶层 Artifacts / References 按 `path` 聚合去重；轮次内按 `turn_key + path` 聚合去重。
- 对同一 `path` 的多次命中，保留最近 `status`，并可追加 `hits[]` 来源信息。
- `todos` 使用**当前轮**最新快照，不做跨轮历史。实时阶段如果工具结果只包含部分 todo，后端在 live 状态按 `id` 合并；缺失 `content` 或返回 `(no description)` 时保留旧内容。对外 SSE/GET 仅下发带可展示 `content` 的条目；首轮仅 `id/status` 时不带 `todos` 字段。
- 字段缺失时保持为空或跳过，不从相似字段自动补全。
- 前端收到重复 `sequence` 或重复 `turn_key + path + source.tid` 时必须幂等处理。

## 工具解析矩阵

工具解析是显式白名单，不按工具名相似性、字段名相似性或助手正文自动推断。

### Tasks

- 解析工具：`todo`。
- 解析时机：
  - `tool_start`：不解析 todos，因为工具尚未返回最终 `todos[]`。
  - `tool_complete`：解析工具结果中明确存在的顶层 `todos[]`，发送 `manifest_delta.todos`。
  - `/api/session/manifest` 构建：仅在**当前轮**（最后一个 `role=user` 之后）扫描 `role='tool'` 消息中的顶层 `todos[]`，按 `id` 合并；无 todo 工具则 `todos.items` 为空。
- 字段来源：
  - 只认工具结果 JSON 的顶层 `todos[]`。
  - 每个 todo 只取 `id`、`content`、`status`。
  - 未识别的非空 status 标为 `unknown`。
- 不解析 assistant prose 里的 todo 列表、markdown checklist、其它工具返回里碰巧含有相似字段的内容。

### Artifacts

- 解析工具（workspace 写入类）：`write_file`、`create_file`、`edit_file`、`patch`、`apply_patch`、`mcp_filesystem_write_file`、`mcp_filesystem_edit_file`。
- 解析工具（skill 成果）：
  - `skill_manager_tool`：`skill_manage` 且 `action` 为 `create` / `edit` / `patch` / `write_file` — 写入 `artifacts[]`，`preview: "skill"`；completed 时优先用 result JSON 的 `path`。
  - 通用写入类工具（见上）直接写入 profile `{HERMES_HOME}/skills/.../SKILL.md` 时同样写入 `artifacts[]`，`path` 为相对 skills 根的技能名；仅 `SKILL.md` 触发，skills 下其它文件不算 skill 成果。
  - 校验 session profile skills 目录下 `SKILL.md` 存在；`delete` / `remove_file` 不算成果；`in_progress` 不 wire。
- 解析来源（MEDIA 交付）：`media`（仅 `role=assistant` 正文中的本地 `MEDIA:` 标记；不解析 `role=user`、远程 URL、工具 JSON）。
- 解析时机：
  - `tool_start`：仅从写入类工具参数解析路径，生成 `status='in_progress'` 的 delta。
  - `tool_complete`：再次解析写入类参数/结果/diff；**不产生** MEDIA 条目（此时尚无 assistant `MEDIA:` 正文）。
  - `turn_complete`（SSE）：assistant 消息落盘后、`done` 前，从**本轮** assistant 消息解析 `MEDIA:` → `manifest_delta`（`source.kind=turn_complete`）。
  - `/api/session/manifest` 构建：从持久化 assistant/tool 消息、`session.tool_calls` 与全会话 assistant `MEDIA:` 重建 artifacts，并归属到对应 turn。
- 字段来源：
  - 工具参数里的明确路径字段，如 `path`、`file_path`、`target`、`destination`、`filename`、`paths[]`、`edits[].path`。
  - unified diff 中的 `+++ b/path` / `--- a/path`。
  - ApplyPatch 文本中的 `*** Add File:` / `*** Update File:`。
  - assistant 正文 `MEDIA:([^\s\)\]]+)`（跳过含 `://` 的 ref）。
- 不解析只读工具结果、无 `MEDIA:` 标记的 assistant prose、全 workspace 扫描结果，以及 `.git`、`node_modules`、虚拟环境、构建目录等忽略路径。workspace 外路径**仅**在 `source_tool=media` 时可预览列出。

### References

- 解析工具：`read_file`、`open_file`、`view_file`、`mcp_filesystem_read_file`、`skill_view`。
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
| `turn_complete` | 不解析 | 从本轮已落盘 assistant 消息解析本地 `MEDIA:` | 不解析 |
| `/api/session/manifest` | 从持久化 tool 消息按 `id` 合并 `todos[]` | 从持久化工具活动 + assistant `MEDIA:` 重建全会话和轮次 artifacts | 从持久化工具活动重建全会话和轮次 references |
| SSE replay | 重放已 journaled 的 `manifest_delta`，前端幂等合并 | 重放已 journaled 的 `manifest_delta`，前端幂等合并 | 重放已 journaled 的 `manifest_delta`，前端幂等合并 |

## 设计约束（实现时必须遵守）

1. **派生而非权威**：不替代 transcript；不以 manifest 驱动 Agent 执行。
2. **Artifacts ⊆ 写入工具产物 ∪ assistant `MEDIA:` 本地交付物**：跨轮次聚合去重，不混入全 workspace、不混入只读访问。
3. **References ⊆ 全会话实际读取的内容来源**：跨轮次聚合去重，搜索命中、目录列表、助手 prose 提到的路径都不默认算参考。
4. **Tasks = 合并后的最新 `todo` 快照**：不是 todo 历史；局部更新按 `id` 合并。
5. **路径安全**：workspace 内相对路径 + 既有预览边界；workspace 外绝对路径仅 `source_tool=media` 且文件存在时可列出，预览走 `/api/media`（须已在 assistant 正文中出现）。
6. **字段诚实**：无明确来源则不跨字段推断、不自动复制相似字段填坑。
7. **SSE delta 是乐观派生状态**：只用于流式实时展示；本轮完成后以后端持久化 manifest 为准。

## 已知后续方向

- 扩展更多实际读取/写入工具别名时，需要同步更新本文件的工具解析矩阵和 contract tests。
- 若未来要展示每轮 References，也应继续使用 `turns[]`，不要从搜索命中或助手正文推断。
