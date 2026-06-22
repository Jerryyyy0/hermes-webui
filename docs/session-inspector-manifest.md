# Session Inspector Manifest

Session Inspector Manifest 是从会话工具活动派生出的轻量索引，用于右侧
Workspace Inspector 的 Tasks、Artifacts、References 面板，以及聊天区每轮的
artifact chips。它不是 transcript、执行 journal，也不是 workspace 全量文件列表。

## HTTP 与 SSE

持久化视图由 `GET /api/session/manifest?session_id=<sid>` 返回。流式过程中，
后端会通过聊天 SSE 发送 `manifest_delta`，前端先乐观合并，`done` 后再以 HTTP
manifest 覆盖。

`manifest_delta` 示例：

```json
{
  "version": 1,
  "session_id": "abc123",
  "stream_id": "stream-xyz",
  "turn_key": "turn:4",
  "sequence": 7,
  "source": {
    "kind": "tool_complete",
    "tool": "write_file",
    "tid": "call-1",
    "status": "completed"
  },
  "artifacts": [
    {
      "path": "report.md",
      "preview": "file",
      "source_tool": "write_file"
    }
  ],
  "references": []
}
```

## 聊天区

聊天区使用 `turn_key` 将 `manifest.turns[]` 的 artifacts 渲染到对应轮次。
SSE delta 只更新 Inspector 的乐观状态；最终展示以 `done` 后的 HTTP manifest
为准。

## 工具解析矩阵

| 事件 | Tasks | Artifacts | References |
| --- | --- | --- | --- |
| `tool_start` | 不更新 | 写入工具可预告路径 | 读取工具可预告路径 |
| `tool_complete` | `todo` 工具解析 `todos[]` | `write_file`、`edit_file`、`patch`、`MEDIA:` 等写入或交付文件 | `read_file`、技能查看等实际读取来源 |
| `turn_complete` | 不更新 | 本轮缺失 artifact 可由 reconcile/backfill 补齐 | 不更新 |

Artifacts 的权威持久化来源是 profile-aware artifact store。Transcript/tool/prose
回扫只用于缺失记录的 backfill，不覆盖 store 已有记录。
