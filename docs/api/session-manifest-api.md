# Session Manifest HTTP/SSE 契约

本文定义 Session Manifest 的对外 HTTP/SSE 字段、合并和生命周期契约。产品语义见 [session-inspector-manifest.md](./session-inspector-manifest.md)；Artifacts 内部提取与持久化见 [session-manifest-artifacts.md](./session-manifest-artifacts.md)。

实现入口：`api/routes.py`（HTTP）、`api/session_manifest.py`（构建与 delta）、`api/streaming.py` / `api/gateway_chat.py`（SSE）、`static/workspace.js`（前端缓存）。

## 1. GET `/api/session/manifest`

### 请求

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | query | 是 | 会话 ID |

```http
GET /api/session/manifest?session_id=abc123
```

### 成功响应 `200`

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
          "page_content": ["第一章 总则……"]
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

### `todos`

```json
{
  "items": [
    { "id": "plan", "content": "Implement", "status": "completed" }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 合并键 |
| `content` | string | 可展示任务说明；空值不出站 |
| `status` | string | `pending`、`in_progress`、`completed`、`cancelled` 或 `unknown` |

Todos 是当前轮最新快照，不是历史流水。GET 中不包含 SSE 专用的 `mode`。

### Artifact row

基础字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | 见下方 path 语义；或允许的 media 绝对路径；或 canonical skill 名 |
| `preview` | string | `file` 或 `skill` |
| `source_tool` | string | 明确 provenance，不为空 |

可选字段：

| 字段 | 适用范围 | 说明 |
| --- | --- | --- |
| `profile` | artifacts | 来自 `session.profile`；无明确值时省略 |
| `status` | artifacts | 当前仅 `expired`，表示有历史 provenance 但不可预览 |

**`preview=file` 的 `path` 语义（wire）：** 持久化 artifact 以自身的 `workspace_root` 解析；历史 `workspace_root=""` 在运行时解释为启动时的 `HERMES_WEBUI_DEFAULT_WORKSPACE`，数据库原值不回填。当该根位于 integration 根之下时，GET/SSE 返回**相对 integration 根**的路径，以便直接调用 `GET /api/integration/workspace/file?path=...`：历史默认根为 `report.md`，新 managed 会话为 `sessions/<session_id>/report.md`，base 子目录 external workspace 为 `project-a/report.md`。根位于 integration 根之外的持久化 artifact 不返回 `preview=file`，不能退回裸相对路径或绝对路径。仅尚未持久化的 transcript/SSE 临时行沿用当前 session workspace 的既有投影。`preview=skill` 与 workspace 外绝对 `MEDIA:` path 不改写。

Manifest 不返回文件或技能正文。非 expired 且 `preview` 为 `file`/`skill` 的条目可由 `HermesSessionInspector.openManifestPreview(item)` 打开；expired 条目不可预览。

文件预览使用 integration workspace file API（path 为上表 wire 语义），skill 预览使用 SkillHub content API，workspace 外 `MEDIA:` 使用 session media API。具体接口与部署约束见 [integration/README.md](../integration/README.md)。

### Reference row

文件读取、搜索命中、目录列表与助手正文提及均不进入 references。当前仅有两类：成功 completed 的 `skill_view`，以及以下两个 MCP 工具的成功 completed 调用：

- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments`
- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross`

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
    "page_content": ["第一章 总则……"]
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | `skill` 或 `knowledge_base_document` |
| `source` | object[] | 来源工具与工具调用 ID；`skill` 使用 `skill_view` |
| `metadata.path` | string | `kind=skill` 时的 canonical skill 名 |
| `metadata.kbName` | string | `kind=knowledge_base_document` 时的知识库名 |
| `metadata.fileName` | string | 文档文件名；单库工具从其私有 `metadata.source` 仅取 basename，不向浏览器透传原路径 |
| `metadata.page_content` | string[] | 文档命中片段，保留工具返回顺序并去重 |

知识库工具结果仅接受最多 50 条、总编码不超过 1 MiB 的 JSON 列表；畸形、失败、未知工具或超限结果一律跳过。Reference 是 transcript/tool-call 派生索引，不写入 artifact store，因此无需新增数据库表。

### `turns[]`

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
        "page_content": ["第一章 总则……"]
      }
    }
  ]
}
```

`turn_key` 优先使用持久化的 `user._turn_key`；只有完全没有稳定 key 的历史 transcript 才 fallback 为 `turn:<user_msg_idx>`。混合 keyed/unkeyed transcript 不生成新的 `turn:N`。SSE 和聊天 `data-turn-key` 必须使用同一个 key。聊天区 per-turn chips 只消费 `turns[].artifacts`。

### `diagnostics`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `missing_turn_key_message_indices` | integer[] | 混合 transcript 中缺少稳定 key 的 user 消息索引；GET 不自动补号 |
| `orphan_turn_keys` | string[] | Store 中存在 artifact、但 transcript 没有同 key user anchor 的历史轮次 |

Orphan artifact 仍保留在顶层 `artifacts`，但不进入正常 `turns[]`。

## 3. SSE `manifest_delta`

与聊天流共用 SSE 连接，事件名为 `manifest_delta`。

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
| `version` | 协议版本，当前为 `1` |
| `session_id` | 前端丢弃非当前会话事件 |
| `stream_id` | 配合 `sequence` 做幂等和过期流过滤 |
| `turn_key` | stream 启动时确定；前端不得从 `stream_id` 推断 |
| `sequence` | 单 stream 内单调递增 |
| `source.kind` | `tool_start`、`tool_complete` 或 `turn_complete` |
| `source.tool` / `tid` / `status` | provenance、展示和去重信息 |
| `todos` | 可选；SSE 可额外含 `mode: "replace_latest"` |
| `artifacts` / `references` | 可选；row schema 与 GET 相同 |

`todos`、`artifacts`、`references` 均为空时不发送 delta。

### 知识库 MCP 示例

以下是 `searchKnowledgeBaseDocuments` 成功完成时的完整 SSE 帧。`source` 描述本次
delta 的触发事件，因此 `source.tool` 使用内部规范化的小写工具名；具体文档的来源保留在
`references[].source[]`，使用完整的 MCP 工具名和调用 ID。

```text
event: manifest_delta
data: {"version":1,"session_id":"5ccfb09bb7a7","stream_id":"stream-xyz","turn_key":"turn:4","sequence":12,"source":{"kind":"tool_complete","tool":"mcp__ithink_kb_mcp__searchknowledgebasedocuments","tid":"call_00_nvU4eugjt9ji9RA6gxWu6032","status":"completed"},"artifacts":[],"references":[{"kind":"knowledge_base_document","source":[{"tool":"mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments","tid":"call_00_nvU4eugjt9ji9RA6gxWu6032"}],"metadata":{"kbName":"share49","fileName":"电力市场运行基本规则.docx","page_content":["第一章 总则……"]}}]}

```

同一调用返回多个文档时，`references[]` 包含多条行；同一 turn 内两个受支持工具命中同一
`(kbName, fileName)` 时，客户端按既有合并规则合并为一条，追加不重复的
`source[]` 与 `metadata.page_content[]`。

### 发射阶段

| 阶段 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不发射 | 不发射工具 artifact | 不发射 |
| `tool_complete` | 成功 `todo` 顶层 `todos[]` | 仅成功工具的参数、结果、diff 或 terminal 输出操作数 | 成功 `skill_view`，或两个受支持知识库 MCP 工具的有效结果 |
| `turn_complete` | 不发射 | 工具强证据、`MEDIA:` 与最后一条 assistant 的严格 workspace 文件提取 | 不发射 |

`tool_start` 只可携带待配对的工具参数，永不产生 artifact/reference。`tool_complete` 仅在与同一 `stream_id + tid` 的 start 配对且成功时解析工具参数；complete 缺参数且没有对应 start、或配对身份不一致时不产生工具 artifact。`turn_complete` 中的最终 assistant 裸文件名按当前 turn 强证据、此前 turn 已确认 artifacts、workspace 根目录的唯一 exact-basename 顺序解析；因此后续纯问答 turn 明确列出可唯一解析的既有文件时，可产生该 turn 的 `assistant_prose` artifact。

SSE 是乐观派生状态，不写入 transcript，不进入模型上下文，只更新 Inspector；不直接生成聊天区 per-turn chips。

## 4. 合并、去重与幂等

| 对象 | 规则 |
| --- | --- |
| Session artifacts | 按 profile + canonical path 去重 |
| Session references | Skill 按 canonical skill path；知识库文档按 `(kbName, fileName)` 去重 |
| Turn artifacts/references | 按 `turn_key` 合并；artifact 按 path，reference 按其身份键去重 |
| 知识库文档 | 同文档合并 `source[]` 与 `metadata.page_content[]`，均保持首次出现顺序 |
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
