# Session Manifest HTTP/SSE 契约

Session Manifest 是会话活动的轻量派生索引，用于展示 Tasks、Artifacts、References 与每轮成果。它不是聊天 transcript、执行日志或 workspace 文件列表。

同一资源只保留一个主归类，优先级为 `artifacts > references`。未列出的字段或缺失字段不得推断；客户端应忽略未知字段。

## 1. GET `/api/session/manifest`

### 请求

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | query | 是 | 会话 ID |

```http
GET /api/session/manifest?session_id=abc123
```

### 成功响应 `200`

下面示例表示：当前轮正在撰写报告，已经生成一个文件，并查看了一个 Skill。顶层字段是整个会话的资源集合，`turns` 则标明资源属于哪一轮。

```json
{
  "manifest": {
    "todos": {
      "items": [
        { "id": "draft", "content": "撰写报告", "status": "in_progress" }
      ]
    },
    "artifacts": [
      { "path": "reports/result.md", "preview": "file", "source_tool": "write_file" }
    ],
    "references": [
      { "kind": "skill", "source": [{ "tool": "skill_view", "tid": "call-1" }], "metadata": { "path": "research" } }
    ],
    "turns": [
      {
        "turn_key": "turn:4",
        "artifacts": [{ "path": "reports/result.md", "preview": "file", "source_tool": "write_file" }],
        "references": [{ "kind": "skill", "source": [{ "tool": "skill_view", "tid": "call-1" }], "metadata": { "path": "research" } }]
      }
    ]
  },
  "manifest_source": "db"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `manifest` | object | 当前会话的完整 Manifest。 |
| `manifest_source` | string | `db`、`none` 或 `unknown`。`none` 时 artifacts 为空，但 references 仍可能存在。 |

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `session_id` |
| `404` | 会话不存在 |
| `500` | Manifest 构建失败 |

## 2. Manifest schema

### `todos`

```json
{
  "items": [
    { "id": "draft", "content": "撰写答复", "status": "in_progress" }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 任务稳定标识。 |
| `content` | string | 展示文本。 |
| `status` | string | `pending`、`in_progress`、`completed`、`cancelled` 或 `unknown`。 |

### `artifacts[]`

`manifest.artifacts` 是会话级完整集合；`turns[].artifacts` 是对应轮次的完整集合。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | 成果的服务端返回路径；客户端不得自行改写。 |
| `preview` | string | `file` 或 `skill`。 |
| `source_tool` | string | 产生该成果的工具或来源。 |
| `profile` | string | 可选，所属 Profile。 |
| `status` | string | 可选；当前仅 `expired`，表示保留历史记录但不可预览。 |

`status: "expired"` 的条目应继续展示，但不得提供预览操作。文件、技能和媒体预览接口见 [integration README](../../integration/README.md)。

示例：模型通过 `write_file` 生成报告时，外部前端可按 `preview: "file"` 将其作为文件成果展示。

```json
{
  "path": "reports/result.md",
  "preview": "file",
  "source_tool": "write_file"
}
```

### `references[]`

`manifest.references` 是会话级完整集合；`turns[].references` 是对应轮次的完整集合。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | `skill` 或 `knowledge_base_document`。 |
| `id` | string | 知识库文档的稳定引用 ID；Skill 不提供该字段。 |
| `source` | object[] | 来源工具和调用 ID：`{ "tool": string, "tid": string }`。 |
| `metadata.path` | string | Skill 的 canonical 名称。 |
| `metadata.kbName` | string | 知识库名称。 |
| `metadata.fileName` | string | 知识库文档名。 |
| `metadata.chunks` | object[] | 最终答案实际采用的知识库片段。 |
| `metadata.chunks[].id` | string | 稳定 chunk ID。 |
| `metadata.chunks[].page_content` | string | 原始片段内容。 |
| `metadata.chunks[].score` | number \| string | 原始评分；无有效值时为 `""`。 |
| `status` | string | 可选；Skill 当前仅可能为 `expired`。 |

知识库检索结果不会立即成为 reference。只有最终 assistant 答复实际采用的片段才会出现在 Manifest 中。

示例：知识库文档引用包含稳定文档 ID 和实际采用的片段。用 `id` 关联引用，用 `chunks` 呈现引用内容。

```json
{
  "kind": "knowledge_base_document",
  "id": "kbdoc:v1:Fb8...",
  "source": [{ "tool": "knowledge_search", "tid": "call-kb-1" }],
  "metadata": {
    "kbName": "market-rules",
    "fileName": "rules.docx",
    "chunks": [{ "id": "kbchunk:v1:Q3c...", "page_content": "第一章 总则……", "score": 0.82 }]
  }
}
```

### `turns[]`

```json
{
  "turn_key": "turn:4",
  "artifacts": [],
  "references": []
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `turn_key` | string | 该 user turn 的稳定标识，应作为不透明字符串使用。 |
| `artifacts` | array | 本轮完整、去重后的成果集合。 |
| `references` | array | 本轮完整、去重后的引用集合。 |

GET 返回全部已有 turns。无对应 user turn 的 artifact 仍会保留在顶层 `artifacts`，但不会出现在 `turns[]`。

`POST /api/session/truncate` 可传布尔字段 `regenerate: true`，此时 `keep_count` 定位待重生成
assistant 所属真实 user 轮次。服务端立即删除该 user turn 及其后续 transcript、context、tool calls、委派
记录与 Artifact rows；不删除磁盘成果文件，也不保留旧 Manifest 或后续 turns 供失败回滚。客户端应使用响应中的
`session.messages` 立即重绘。
重生成响应额外返回 `last_user_text`（string），为服务端确认的原始真实用户问题，客户端应使用该字段重新发送，
避免分页或内部控制消息影响问题选择。文本匹配时服务端一次性复用原 turn key；失败、取消、刷新或重启不恢复
被删除的历史；服务端也会清除本次裁剪产生的 transcript `.json.bak`，避免启动恢复撤销主动重生成。
普通裁剪请求语义不变。

## 3. SSE `manifest_delta`

`manifest_delta` 与聊天流共用 SSE 连接。

以下是同一 stream 的连续两帧。第二帧包含此前的 `result.md` 与新生成的 `chart.png`，因此客户端应整体替换，而不是追加第二帧中的条目。

```text
event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":9,"artifacts":[{"path":"reports/result.md","preview":"file","source_tool":"write_file"}],"references":[],"turns":[{"turn_key":"turn:4","artifacts":[{"path":"reports/result.md","preview":"file","source_tool":"write_file"}],"references":[]}]}

event: manifest_delta
data: {"version":2,"session_id":"abc123","stream_id":"stream-xyz","turn_key":"turn:4","sequence":10,"artifacts":[{"path":"reports/chart.png","preview":"file","source_tool":"write_file"},{"path":"reports/result.md","preview":"file","source_tool":"write_file"}],"references":[],"turns":[{"turn_key":"turn:4","artifacts":[{"path":"reports/chart.png","preview":"file","source_tool":"write_file"},{"path":"reports/result.md","preview":"file","source_tool":"write_file"}],"references":[]}]}
```

| 字段 | 类型 | 客户端处理 |
| --- | --- | --- |
| `version` | integer | 当前为 `2`。 |
| `session_id` | string | 丢弃非当前会话事件。 |
| `stream_id` | string | 与 `sequence` 共同标识事件。 |
| `sequence` | integer | 同一 stream 内单调递增；重复的 `stream_id:sequence` 必须忽略。 |
| `turn_key` | string | 当前轮稳定标识。 |
| `todos` | object | 可选；`items` 是当前轮最新完整快照，直接替换。 |
| `artifacts` | array | 当前会话完整、去重快照，直接替换顶层 artifacts。 |
| `references` | array | 当前会话完整、去重快照，直接替换顶层 references。 |
| `turns` | array | 仅含当前 `turn_key` 的完整快照，直接替换该 turn 行。 |

不要在客户端合并、去重或根据旧 SSE payload 补全 resources。每次 `manifest_delta` 的顶层 `artifacts` / `references` 和当前 turn 行都可直接替换。

知识库引用不会通过实时 SSE 确认。stream 完成后，重新请求 GET Manifest 以获得最终采用的知识库文档与片段。

## 4. 生命周期

| 时机 | 推荐客户端行为 |
| --- | --- |
| 打开或切换会话 | 请求 GET Manifest，并整体替换本地状态。 |
| 收到 `manifest_delta` | 按 `stream_id:sequence` 去重后，按上表替换字段。 |
| 当前 stream 结束 | 再次请求 GET Manifest，使用持久化结果覆盖 SSE 乐观状态。 |

SSE 只反映运行中的乐观状态，不替代 GET 的最终结果。

## 5. 最小客户端伪代码

```js
if (seen.has(`${delta.stream_id}:${delta.sequence}`)) return;
seen.add(`${delta.stream_id}:${delta.sequence}`);

manifest.todos = delta.todos ?? manifest.todos;
manifest.artifacts = delta.artifacts;
manifest.references = delta.references;

const turn = delta.turns[0];
if (turn) {
  const index = manifest.turns.findIndex(item => item.turn_key === delta.turn_key);
  if (index >= 0) manifest.turns[index] = turn;
  else manifest.turns.push(turn);
}
```
