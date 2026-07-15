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
        "path": "research-skill",
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
  "manifest_source": "db"
}
```

响应不包含顶层 `session_id`、`workspace`、`counts` 或独立 `live` 字段。活跃 stream 的乐观 manifest 会在服务端合并进 `manifest` 后返回。

### `manifest_source`

| 值 | 含义 |
| --- | --- |
| `db` | 使用 artifact store 中的非空或 empty decision；empty turn 可能执行严格 read-repair |
| `backfill` | 当前 lineage 原先完全无 decision，本次从 legacy JSON 或 transcript 写回 |
| `derived` | 无法写入 DB，仅临时从 transcript 派生 |
| `unknown` | 异常或无法判断 |

GET read-repair 不得更新 session `updated_at`、sidebar recency，也不得发布 session-list 变更事件。

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

### Artifact/reference row

References 仅允许 canonical skill row：`preview="skill"`、`source_tool="skill_view"`。文件读取不进入 GET/SSE/per-turn references，也不产生 file reference `expired`。

基础字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | workspace 相对路径、允许的 media 绝对路径或 canonical skill 名 |
| `preview` | string | `file` 或 `skill` |
| `source_tool` | string | 明确 provenance，不为空 |

可选字段：

| 字段 | 适用范围 | 说明 |
| --- | --- | --- |
| `profile` | artifacts | 来自 `session.profile`；无明确值时省略 |
| `status` | artifacts/references | 当前仅 `expired`，表示有历史 provenance 但不可预览 |

Manifest 不返回文件或技能正文。非 expired 且 `preview` 为 `file`/`skill` 的条目可由 `HermesSessionInspector.openManifestPreview(item)` 打开；expired 条目不可预览。

文件预览使用 integration workspace file API，skill 预览使用 SkillHub content API，workspace 外 `MEDIA:` 使用 session media API。具体接口与部署约束见 [integration/README.md](../integration/README.md)。

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
  "references": []
}
```

`turn_key` 优先使用持久化的 `user._turn_key`；只有历史 transcript 缺少稳定 key 时才 fallback 为 `turn:<user_msg_idx>`。SSE 和聊天 `data-turn-key` 必须使用同一个 key。聊天区 per-turn chips 只消费 `turns[].artifacts`。

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

### 发射阶段

| 阶段 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不发射 | 不发射工具 artifact | 不发射 |
| `tool_complete` | 成功 `todo` 顶层 `todos[]` | 仅成功工具的参数、结果、diff 或 terminal 输出操作数 | 仅明确 `success: true` 的 `skill_view` |
| `turn_complete` | 不发射 | 工具强证据、`MEDIA:` 与最后一条 assistant 的严格 workspace 文件提取 | 不发射 |

SSE 是乐观派生状态，不写入 transcript，不进入模型上下文，只更新 Inspector；不直接生成聊天区 per-turn chips。

## 4. 合并、去重与幂等

| 对象 | 规则 |
| --- | --- |
| Session artifacts | 按 profile + canonical path 去重 |
| Session references | 只接受 canonical skill identity |
| Turn artifacts/references | 按 `turn_key` 合并；artifact 按 path、reference 按 canonical skill 去重 |
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
