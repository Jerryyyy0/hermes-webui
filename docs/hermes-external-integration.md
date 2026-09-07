# 外部集成接缝

本 Fork 的外部集成实现位于 `integration/`。上游文件只保留必要的导入、注册或
参数传递，以降低上游同步冲突。

## Runtime configuration

`integration/env_config/` 提供只读的 `GET /api/integration/config`。接口只返回白名单
环境变量 `BROWSER_PREVIEW_URL` 的当前进程值，不枚举其它环境变量；
`api/routes.py` 仅保留 GET handler 的薄委派。该接口仅在 `HERMES_INTEGRATION=1`
时启用。

## Workspace file overwrite

`integration/workspace/` 还提供 `POST /api/integration/workspace/file/overwrite`。
该 multipart 接口在 `api/routes.py` 的 POST body 解析前通过 early hook 薄委派，
仅覆盖 integration workspace 内已有的相对普通文件；绝对路径和外部 Manifest
Artifact 继续保持只读。保存实现、路径安全和文件索引失效均由 integration 层负责。

## External Manifest Artifact References

`integration/session_manifest/external_references/` 承载外部绝对路径 Artifact 的策略、安全打开、登记
record 查询与预览授权：`policy.py` 负责逐组件无跟随 fd 打开与受保护路径拒绝（已登记的会话附件与
`HERMES_HOME/memories/` 文件是只读预览例外，上传本身不产生授权），`references.py`
负责根据既有 row 推导引用资格，`preview.py` 复用原有 workspace 文件字节流服务。它不复制、移动或
哈希源文件，也不新增 SQLite 字段。

允许的接缝是 `integration/session_manifest/manifest.py`（候选归一化与 wire 投影）、`api/routes.py`（接受已打开 fd 的
通用字节流函数）、`integration/workspace/handlers.py`（既有 URL 的绝对路径薄分支）和
`static/workspace.js`（外部路径只读 UI）。`api/streaming.py`、`api/gateway_chat.py` 与 store 不承载
外部路径策略或额外 SQL。公开 URL、参数和 Manifest/SSE schema 不变。

## Cron Workspace

`integration/crons/` 负责 Cron workspace policy、当前执行物化、生命周期与 Cron
Hub 状态。允许修改的上游接缝如下：

- `api/routes.py`：Cron handler、运行完成钩子与 session GET 锁内 transcript 重协调的薄委派。
- `api/models.py`：`import_cli_session()` 接收显式 workspace binding。
- `api/workspace.py`：通过 `resolve_session_workspace()` 集中将 V1 未验证 Cron
  workspace 降级为 WebUI 已批准的默认 workspace，而不使用未验证 root；legacy
  `external/legacy_shared` 绑定可继续使用其已批准的共享目录。

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
搜索工具结果归一化为 chunk candidates；`integration/knowledge_base/citations.py` 负责
provider-only `_cite`、最终回答结算和已提交 Citation 投影。检索命中本身不构成公开
Manifest reference；只有实际引用的 chunk 才随最终 assistant message 保存在既有
Session `session.json` 中。不新增数据库表、sidecar 或独立 Citation JSON。

`integration/session_manifest/manifest.py` 将 completed ToolEvent 交给解析器，但 live delta 会过滤
知识库 candidates；`GET /api/session/manifest` 只从自洽的最终 message Citation/evidence
生成 references wire。该模块还在 stream 起始处以持久化 Manifest reference 为基线，并在出站前生成
完整、已去重的顶层 `references` 快照；`api/streaming.py` 与 `api/gateway_chat.py` 仅保留该共享投影的
调用、hook、保存和出站过滤接缝，不解析 MCP JSON；前端直接替换该快照，不重复解析或合并。

公开字段、SSE 帧和生命周期只在 [Session Manifest HTTP/SSE 契约](api/session-manifest-api.md)
维护；本节只记录 Fork 接缝与实现边界。
