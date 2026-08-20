# 外部集成接缝

本 Fork 的外部集成实现位于 `integration/`。上游文件只保留必要的导入、注册或
参数传递，以降低上游同步冲突。

## Cron Workspace

`integration/crons/` 负责 Cron workspace policy、当前执行物化、生命周期与 Cron
Hub 状态。允许修改的上游接缝如下：

- `api/routes.py`：Cron handler 与运行完成钩子的薄委派。
- `api/models.py`：`import_cli_session()` 接收显式 workspace binding。
- `api/workspace.py`：通过 `resolve_session_workspace()` 集中拒绝 V1 未验证的 Cron
  workspace；legacy `external/legacy_shared` 绑定可继续使用其已批准的共享目录。

Cron 专属路径解析、policy 判断、清理逻辑和 UI 状态不得复制到这些接缝文件。

## Async Delegation

`integration/async_delegation_turns/` 负责后台 delegation 的归属 sidecar、每轮 user
message 状态投影、取消屏障与会话 SSE 生命周期信封。允许修改的上游接缝如下：

- `api/models.py`：持久化 `async_delegation_cancellation`。
- `api/streaming.py`：在 chat stream 内写入派发归属并发送唯一的
  `background_task_dispatched` 通知。
- `api/background_process.py`：处理 completion、取消屏障、未解析终态与 wakeup 抑制。
- `api/routes.py`：委派取消 HTTP handler，并使按 session SSE 在保留 run-journal
  行为的同时发送首个后台任务快照。

HTTP handler、状态变更和事件构造必须留在 `integration/async_delegation_turns/`，不得在
这些上游接缝中复制业务逻辑。

## Knowledge Base Manifest References

`integration/knowledge_base/turn_references.py` 负责将 IThink 知识库 MCP 的两个精确
搜索工具结果归一化为 Session Manifest references，并按 `(kbName, fileName)` 聚合。
解析、大小限制、路径脱敏和 wire projection 都必须留在该目录；不新增数据库表或
sidecar 状态。

允许的上游接缝只有 `api/session_manifest.py`：它把已配对的 completed ToolEvent 交给
解析器，并把结果放入既有 `manifest_delta` SSE 与 `GET /api/session/manifest` 的同一
references wire。不得在 `api/streaming.py` 或前端重复解析 MCP 结果。
