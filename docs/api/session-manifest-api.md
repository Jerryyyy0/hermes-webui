# Session Manifest HTTP/SSE 契约

本文是 Session Manifest 的唯一对外契约：定义 HTTP/SSE 字段、资源语义、合并和生命周期。Artifacts 的内部提取、持久化和路径安全见 [Session Manifest Artifacts 实现](../architecture/session-manifest-artifacts.md)；`turn_key` 的生成、压缩与消息层对齐见 [Turn Key 后端说明](../architecture/turn-key-backend.md)。

实现入口：`api/routes.py`（HTTP）、`integration/session_manifest/manifest.py`（构建与 delta）、`api/streaming.py` / `api/gateway_chat.py`（SSE）、`static/workspace.js`（前端缓存）。

## 0. Manifest 的定位与资源边界

Session Manifest 是从会话活动派生出的轻量索引，服务于 Workspace Inspector 的 Tasks、Artifacts、References，以及聊天区每轮的 artifact chips。它不是 transcript、执行 journal，也不是 workspace 文件清单。

| 数据 | 含义 | 明确排除 |
| --- | --- | --- |
| Tasks | 当前轮 `todo` 工具产生的最新任务快照 | 历史流水、助手正文中的列表 |
| Artifacts | 当前会话由明确成果证据创建、修改或交付的文件/技能 | 搜索命中、目录列表、输入文件、跨字段推断 |
| References | 明确成功 `skill_view` 的技能，或受支持知识库 MCP 返回的文档命中 | 文件读取、搜索、列目录、助手普通提及 |
| Turns | 按真实 user 消息划分的 per-turn artifacts/references 视图 | transcript、完整执行历史、Agent internal scaffold、model-only context anchor |

同一资源在一个 Manifest 中只保留一个主归类，优先级为 `artifacts > references`。缺失字段保持为空或跳过，不从相似字段推断、复制或补全。

Artifacts 只接受成功 completed 工具的结构化证据、显式 `MEDIA:`、成功 skill mutation，或当前 turn 最后一条 assistant 中经严格验证的路径。工具和最终 assistant 的绝对路径可成为外部直接引用；相对路径仍必须位于当前 session workspace。工具 start、文件读取、terminal stdout、目录列表、中间 assistant prose 和全 workspace 扫描不构成 artifact 证据；具体白名单与路径 gate 以 [Artifacts 实现文档](../architecture/session-manifest-artifacts.md) 为准。

References 只接受成功 completed 的 `skill_view` 或本页列出的两个知识库 MCP 工具。知识库 reference 不写入 `session_manifest.db`，也不新增数据库表或 sidecar；artifact store 只负责 artifacts 的成果/空决策。

## 1. GET `/api/session/manifest`

### 请求

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | query | 是 | 会话 ID |

```http
GET /api/session/manifest?session_id=abc123
```

### 成功响应 `200`：完整 Manifest 示例

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
        "path": "reports/result.md",
        "preview": "file",
        "source_tool": "write_file",
        "profile": "ops"
      }
    ],
    "references": [
      {
        "kind": "skill",
        "source": [
          { "tool": "skill_view", "tid": "call-skill-1" }
        ],
        "metadata": { "path": "research-skill" }
      },
      {
        "kind": "knowledge_base_document",
        "source": [
          {
            "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
            "tid": "call-kb-1"
          }
        ],
        "metadata": {
          "kbName": "share49",
          "fileName": "电力市场运行基本规则.docx",
          "chunks": [
            { "page_content": "第一章 总则……", "score": 0.82 }
          ]
        }
      }
    ],
    "turns": [
      {
        "turn_key": "turn:0",
        "artifacts": [],
        "references": []
      }
    ],
    "diagnostics": {
      "missing_turn_key_message_indices": [],
      "orphan_turn_keys": []
    }
  },
  "manifest_source": "db"
}
```

响应不包含顶层 `session_id`、`workspace`、`counts` 或独立 `live` 字段。活跃 stream 的乐观 manifest 会在服务端合并进 `manifest` 后返回。

### `manifest_source`

| 值 | 含义 |
| --- | --- |
| `db` | 使用 artifact store 中的非空或 empty decision |
| `none` | 当前 lineage 无 artifact store decision；`artifacts` 保持空，但仍从已完成工具事件派生 references |
| `unknown` | 异常或无法判断 |

GET 是只读的：不得执行 artifact backfill 或 empty-decision repair，不得更新 session `updated_at`、sidebar recency，也不得发布 session-list 变更事件。没有 artifact store decision 时不重建历史 artifacts；references 不依赖该 store。

### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `session_id` |
| `404` | 会话不存在 |
| `500` | 构建 manifest 失败 |

## 2. Manifest schema

以下所有 JSON 均为实际 wire 形状，可直接作为客户端类型定义、fixture 或 mock 的参考。
未列出的字段不应自行推断或补写。

### `todos`：任务快照

```json
{
  "items": [
    { "id": "research", "content": "检索市场规则", "status": "completed" },
    { "id": "draft", "content": "撰写答复", "status": "in_progress" },
    { "id": "review", "content": "核对引用", "status": "pending" },
    { "id": "obsolete", "content": "旧任务", "status": "cancelled" },
    { "id": "legacy", "content": "旧工具状态", "status": "unknown" }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 合并键 |
| `content` | string | 可展示任务说明；空值不出站 |
| `status` | string | `pending`、`in_progress`、`completed`、`cancelled` 或 `unknown` |

Todos 是当前轮最新快照，不是历史流水。GET 中不包含 SSE 专用的 `mode`；SSE 中的
`todos.mode: "replace_latest"` 表示本次 items 应替换当前轮的任务快照。

### `artifacts[]` / `turns[].artifacts[]`：成果行

基础字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | 见下方 path 语义；可为已登记外部直接引用或允许的 media 绝对路径；或 canonical skill 名 |
| `preview` | string | `file` 或 `skill` |
| `source_tool` | string | 明确 provenance，不为空 |

可选字段：

| 字段 | 适用范围 | 说明 |
| --- | --- | --- |
| `profile` | artifacts | 来自 `session.profile`；无明确值时省略 |
| `status` | artifacts / Skill references | 当前仅 `expired`，表示有历史 provenance 但不可预览 |

#### 文件成果：`preview: "file"`

```json
{
  "path": "sessions/abc123/reports/result.md",
  "preview": "file",
  "source_tool": "write_file",
  "profile": "ops"
}
```

#### 技能成果：`preview: "skill"`

```json
{
  "path": "research/market-analysis",
  "preview": "skill",
  "source_tool": "skill_manage",
  "profile": "ops"
}
```

#### 已过期成果

```json
{
  "path": "reports/deleted.md",
  "preview": "file",
  "source_tool": "write_file",
  "status": "expired"
}
```

`status: "expired"` 仅表示历史成果证据仍在、当前不可预览；客户端必须保留展示但禁用打开。

**`preview=file` 的 `path` 语义（wire）：** 持久化 workspace artifact 以自身的 `workspace_root` 解析；历史 `workspace_root=""` 在运行时解释为启动时的 `HERMES_WEBUI_DEFAULT_WORKSPACE`，数据库原值不回填。当该根位于 integration 根之下时，GET/SSE 返回**相对 integration 根**的路径，以便直接调用 `GET /api/integration/workspace/file?path=...`：历史默认根为 `report.md`，新 managed 会话为 `sessions/<session_id>/report.md`，base 子目录 external workspace 为 `project-a/report.md`。已登记的外部直接引用保留规范化后的绝对 `path`，也通过同一 URL 预览；服务端只接受精确匹配持久化 Artifact row 且当前安全可读的路径。根位于 integration 根之外的普通 workspace artifact 不返回 `preview=file`，不能退回裸相对路径或绝对路径。仅尚未持久化的 transcript/SSE 临时行沿用当前 session workspace 的既有投影。`preview=skill` 与 workspace 外绝对 `MEDIA:` path 不改写。

Manifest 不返回文件或技能正文。非 expired 且 `preview` 为 `file`/`skill` 的条目可由 `HermesSessionInspector.openManifestPreview(item)` 打开；expired 条目不可预览。

文件预览使用 integration workspace file API（path 为上表 wire 语义；外部直接引用为只读），skill 预览使用 SkillHub content API，workspace 外 `MEDIA:` 使用 session media API。具体接口与部署约束见 [integration/README.md](../integration/README.md)。

### `references[]` / `turns[].references[]`：引用行

文件读取、搜索命中、目录列表与助手正文提及均不进入 references。当前仅有两类：成功 completed 的 `skill_view`，以及以下两个 MCP 工具的成功 completed 调用：

- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments`
- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross`

所有引用共用的最小外壳如下：

```json
{
  "kind": "<资源类型>",
  "source": [
    { "tool": "<完整工具名>", "tid": "<工具调用 ID>" }
  ],
  "metadata": {}
}
```

#### Skill 引用：`kind: "skill"`

```json
{
  "kind": "skill",
  "source": [
    { "tool": "skill_view", "tid": "call-skill-1" }
  ],
  "metadata": {
    "path": "research-skill"
  }
}
```

#### 已过期 Skill 引用

```json
{
  "kind": "skill",
  "source": [
    { "tool": "skill_view", "tid": "call-skill-1" }
  ],
  "metadata": {
    "path": "removed-skill"
  },
  "status": "expired"
}
```

#### 知识库文档引用：`kind: "knowledge_base_document"`

```json
{
  "kind": "knowledge_base_document",
  "source": [
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
      "tid": "call-01"
    }
  ],
  "metadata": {
    "kbName": "share49",
    "fileName": "电力市场运行基本规则.docx",
    "chunks": [
      { "page_content": "第一章 总则……", "score": 0.82 }
    ]
  }
}
```

同一文档由两个受支持工具命中后的聚合形状：

```json
{
  "kind": "knowledge_base_document",
  "source": [
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
      "tid": "call-across-1"
    },
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
      "tid": "call-single-1"
    }
  ],
  "metadata": {
    "kbName": "share49",
    "fileName": "电力市场运行基本规则.docx",
    "chunks": [
      { "page_content": "第一章 总则……", "score": 0.82 },
      { "page_content": "第二章 市场成员……", "score": 0.71 }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | `skill` 或 `knowledge_base_document` |
| `source` | object[] | 来源工具与工具调用 ID；`skill` 使用 `skill_view`，知识库文档可聚合多个 MCP 调用 |
| `metadata.path` | string | `kind=skill` 时的 canonical skill 名 |
| `metadata.kbName` | string | `kind=knowledge_base_document` 时的知识库名 |
| `metadata.fileName` | string | 文档文件名；单库工具从其私有 `metadata.source` 仅取 basename，不向浏览器透传原路径 |
| `metadata.chunks` | object[] | 文档命中片段，保留工具返回顺序并按 `page_content` 去重 |
| `metadata.chunks[].page_content` | string | 下游返回的原始内容，不改写 |
| `metadata.chunks[].score` | number or string | 下游返回的原始分数；不存在或无效时为 `""` |
| `status` | string | 可选；当前仅 `expired`，表示 Skill 有历史来源但当前不可预览 |

`score` 是每个命中片段的原始分数，不是筛选阈值；`scoreThreshold` 仍属于知识库搜索请求参数。Manifest 不对分数做归一化，也不改变下游定义的大小关系；下游没有返回有效 score 时统一写为 `""`。

知识库工具结果仅接受最多 50 条、总编码不超过 1 MiB 的 JSON 列表；畸形、失败、未知工具或超限结果一律跳过。Reference 是 transcript/tool-call 派生索引，不写入 artifact store，因此无需新增数据库表。

### `turns[]`：按 user turn 的局部投影

```json
{
  "turn_key": "turn:4",
  "artifacts": [
    {
      "path": "notes.txt",
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": [
    {
      "kind": "knowledge_base_document",
      "source": [
        {
          "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
          "tid": "call-kb-1"
        }
      ],
      "metadata": {
        "kbName": "share49",
        "fileName": "电力市场运行基本规则.docx",
        "chunks": [
          { "page_content": "第一章 总则……", "score": 0.82 }
        ]
      }
    }
  ]
}
```

`turn_key` 优先使用持久化的 `user._turn_key`；只有完全没有稳定 key 的历史 transcript 才 fallback 为 `turn:<user_msg_idx>`。混合 keyed/unkeyed transcript 不生成新的 `turn:N`。SSE 和聊天 `data-turn-key` 必须使用同一个 key。聊天区 per-turn chips 只消费 `turns[].artifacts`。

### `diagnostics`：不影响展示的归属诊断

```json
{
  "missing_turn_key_message_indices": [8, 15],
  "orphan_turn_keys": ["turn:99"]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `missing_turn_key_message_indices` | integer[] | 混合 transcript 中缺少稳定 key 的 user 消息索引；GET 不自动补号 |
| `orphan_turn_keys` | string[] | Store 中存在 artifact、但 transcript 没有同 key user anchor 的历史轮次 |

Orphan artifact 仍保留在顶层 `artifacts`，但不进入正常 `turns[]`。

## 3. SSE `manifest_delta`

与聊天流共用 SSE 连接，事件名为 `manifest_delta`。

### 通用 delta 外壳

```json
{
  "version": 2,
  "session_id": "abc123",
  "stream_id": "stream-xyz",
  "turn_key": "turn:42",
  "sequence": 7,
  "todos": {
    "items": [
      { "id": "plan", "content": "Implement", "status": "completed" }
    ],
    "mode": "replace_latest"
  },
  "artifacts": [
    {
      "path": "reports/result.md",
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": []
}
```

| 字段 | 说明 |
| --- | --- |
| `version` | 协议版本，当前为 `2`；references 使用 `metadata.chunks[]` |
| `session_id` | 前端丢弃非当前会话事件 |
| `stream_id` | 配合 `sequence` 做幂等和过期流过滤 |
| `turn_key` | stream 启动时确定；前端不得从 `stream_id` 推断 |
| `sequence` | 单 stream 内单调递增 |
| `todos` | 可选；SSE 可额外含 `mode: "replace_latest"` |
| `artifacts` / `references` | 可选；row schema 与 GET 相同 |

`todos`、`artifacts`、`references` 均为空时不发送 delta。

每条 delta 至少包含 `version`、`session_id`、`stream_id`、`turn_key`、`sequence`，再携带本次
有变化的 `todos`、`artifacts` 或 `references`。顶层不携带工具调用来源；引用来源只在
`references[].source[]` 中表达。

### Todo delta

```text
event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":8,"todos":{"items":[{"id":"draft","content":"撰写答复","status":"in_progress"}],"mode":"replace_latest"},"artifacts":[],"references":[]}
```

### 文件成果 delta

```text
event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":9,"artifacts":[{"path":"reports/result.md","preview":"file","source_tool":"write_file","profile":"ops"}],"references":[]}
```

### Skill 引用 delta

```text
event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":10,"artifacts":[],"references":[{"kind":"skill","source":[{"tool":"skill_view","tid":"call-skill-1"}],"metadata":{"path":"research-skill"}}]}
```

### 知识库 MCP 示例

以下是 `searchKnowledgeBaseDocuments` 成功完成时的完整 SSE 帧。具体文档的来源保留在
`references[].source[]`，使用完整的 MCP 工具名和调用 ID；delta 顶层不重复携带工具
完成事件字段。

```text
event: manifest_delta
data: {"version":2,"session_id":"5ccfb09bb7a7","stream_id":"stream-xyz","turn_key":"turn:4","sequence":12,"artifacts":[],"references":[{"kind":"knowledge_base_document","source":[{"tool":"mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments","tid":"call_00_nvU4eugjt9ji9RA6gxWu6032"}],"metadata":{"kbName":"share49","fileName":"电力市场运行基本规则.docx","chunks":[{"page_content":"第一章 总则……","score":0.82}]}}]}

```

同一调用返回多个文档时，`references[]` 包含多条行；同一 turn 内两个受支持工具命中同一
`(kbName, fileName)` 时，客户端按既有合并规则合并为一条，追加不重复的
`source[]` 与 `metadata.chunks[]`。

### Turn reconcile 成果 delta

turn reconcile 仅补充 artifacts；它的 `turns[]` 仅含当前 `turn_key` 的局部 artifacts，
references 固定为空。

```text
event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":13,"artifacts":[{"path":"reports/final.docx","preview":"file","source_tool":"assistant_prose"}],"turns":[{"turn_key":"turn:4","artifacts":[{"path":"reports/final.docx","preview":"file","source_tool":"assistant_prose"}],"references":[]}]}
```

### 发射阶段

| 阶段 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不发射 | 不发射工具 artifact | 不发射 |
| `tool_complete` | 成功 `todo` 顶层 `todos[]` | 仅成功工具的参数、结果、diff 或 terminal 输出操作数 | 成功 `skill_view`，或两个受支持知识库 MCP 工具的有效结果 |
| `turn_complete` | 不发射 | 工具强证据、`MEDIA:` 与最后一条 assistant 的严格路径提取 | 不发射 |

`tool_start` 只可携带待配对的工具参数，永不产生 artifact/reference。`tool_complete` 仅在与同一 `stream_id + tid` 的 start 配对且成功时解析工具参数；complete 缺参数且没有对应 start、或配对身份不一致时不产生工具 artifact。`turn_complete` 中的最终 assistant 路径只扫描最后一条真实 assistant message：绝对路径可为外部直接引用；相对路径与裸文件名只在当前 session workspace 解析。外部直接引用需先持久化，随后由 GET Manifest 作为可预览成果返回。

SSE 是乐观派生状态，不写入 transcript，不进入模型上下文，只更新 Inspector；不直接生成聊天区 per-turn chips。

## 4. 合并、去重与幂等

| 对象 | 规则 |
| --- | --- |
| Session artifacts | 按 profile + canonical path 去重 |
| Session references | Skill 按 canonical skill path；知识库文档按 `(kbName, fileName)` 去重 |
| Turn artifacts/references | 按 `turn_key` 合并；artifact 按 path，reference 按其身份键去重 |
| 知识库文档 | 同文档合并 `source[]` 与 `metadata.chunks[]`；chunk 按原始 `page_content` 去重并保持首次出现顺序；同一 chunk 的 score 保留首次有效值 |
| Skills | 同技能 artifact 优先于 skill reference |
| Todos | 当前轮按 `id` 合并；出站前过滤无展示内容项 |
| 缺失字段 | 保持空或跳过，不跨字段推断 |
| SSE 幂等 | 重复 `stream_id:sequence` 忽略 |
| Store replay | 稳定 `tool_call_id` 不得跨 turn 重复归属 |

## 5. 生命周期

| 阶段 | Inspector | 聊天区 chips |
| --- | --- | --- |
| 流式进行中 | 合并 `manifest_delta` 乐观更新 | 不展示 |
| 本轮 `done` 后 | `GET /api/session/manifest` 覆盖 SSE | 使用 `manifest.turns[].artifacts` 刷新 |
| 切换/打开会话 | 拉取 GET manifest | 按 GET 结果渲染 |
| 离开会话 | 清空前端 manifest cache | 清理当前会话绑定 |

## 6. 浏览器 API

`window.HermesSessionInspector`（`static/workspace.js`）：

| 方法 | 说明 |
| --- | --- |
| `refresh()` | 拉取 GET manifest |
| `clear()` | 清空缓存 |
| `applyDelta(delta)` | 幂等合并 SSE delta |
| `getTurnArtifacts(turnKey)` | 获取某轮 artifacts |
| `openManifestPreview(item)` | 非 expired 条目按 `preview` 分发 |
| `isManifestPreviewable(item)` | 排除 expired，并检查 `file`/`skill` |

## 7. 验证

```bash
./scripts/test.sh tests/test_session_manifest.py tests/test_session_manifest_store.py tests/test_session_manifest_contract.py tests/test_session_manifest_replay.py -q
```
