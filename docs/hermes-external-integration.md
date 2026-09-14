# 外部集成接缝

本 Fork 的外部集成实现位于 `integration/`。上游文件只保留必要的导入、注册或
参数传递，以降低上游同步冲突。

## Agent 历史语义与压缩

`api/streaming.py::_sanitize_messages_for_api` 在正常、重试与重建 Agent 的
`conversation_history` 入口显式启用 `preserve_agent_semantics`。
`integration/agent_message_semantics/history.py` 在逐行投影时只保留已识别的
`_hermes_message_class` / `_hermes_scaffold_kind`，并将旧兼容标记规范化；
不扩展 Provider 字段白名单，不透传任意私有字段。Agent transport 在请求副本中
剥离内部字段，Agent 内存历史和压缩持久化则保留语义，展示层继续隐藏内部输入。

状态不变量：压缩保留的内部通知不能变成真实用户轮次；普通用户输入相同正文
仍可见。此修复不扫描 inactive 历史、不改写真实数据库或既有 sidecar，已丢失
标记的历史需另行基于原始记录审计修复，不能按文本前缀批量隐藏。

验证：`./scripts/test.sh integration/tests/agent_message_semantics/test_agent_history.py`。
设置 `HERMES_WEBUI_AGENT_DIR` 指向兼容 Agent checkout 可额外执行真实
SQLite `archive_and_compact` → WebUI 回读/分页 → Agent transport 契约测试；
使用临时数据库，不请求模型。不提供 Agent 时该契约用例跳过。

## Runtime configuration

`api/models.py::_merge_session_display_metadata` 在复制轮次身份与 Agent 语义标签前，
要求既有 content key 严格一致（包含 workspace 前缀归一化）。模糊匹配仅可用于既有
展示去重，不能据此把真实消息标记为内部压缩摘要。相关回归覆盖位于
`integration/tests/agent_message_semantics/test_reconciliation_provenance.py`。

`api/routes.py::_turn_aligned_window_indices` 只复用 Manifest 的 turn 起点；
分页结束位置使用下一有效起点或完整消息尾部，不能直接使用可能留有未绑定行间隙的
Manifest end index，否则会遗漏异步 completion 后的 assistant/tool 消息。

异步交接接缝：`api/process_event_utils.py` 的 claim、ACK、release 使用
`integration/async_delegation_turns/delivery_scope.py`，根据 origin session sidecar
解析 Profile，以 context-local Hermes Home 固定数据库；inbox 回读使用相同作用域。
作用域退出（含异常）恢复原上下文，不修改进程环境变量。

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

重生成相关接缝：`api/session_ops.py` 统一执行破坏式裁剪，`api/routes.py` 接入 truncate/retry 与下一次
chat-start 的一次性原 turn key 复用；`api/streaming.py` 和 `api/gateway_chat.py` 继续走普通终态结算。
`static/ui.js` 只传递重生成标记并使用服务端裁剪结果。目标及后续 turn 的旧后台委派 sidecar 在准备时删除；
不新增 revision 表或旧 Manifest 快照。

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

`integration/async_delegation_turns/` 负责后台 delegation 的归属 sidecar、durable wakeup
inbox、每轮 user message 状态投影、取消屏障与会话 SSE 生命周期信封。完整 prompt 保存在
`async_delegation_origins[delegation_id].wakeup`；Agent ACK 只在该 sidecar 保存成功后发生。
此实现不修改 Agent upstream 的 `async_delegations` SQLite 表结构。允许修改的上游接缝如下：

- `api/models.py`：持久化 `async_delegation_cancellation`，并在 generic stale repair 前调用
  integration recovery hook。
- `api/streaming.py`：在 chat stream 内写入派发归属并发送唯一的
  `background_task_dispatched` 通知；最终 transcript、artifact 与 terminal journal 保存后，
  向 run journal 和 session SSE 发布 `async_turn_committed`，再结算 wakeup。
- `api/background_process.py`：先接管 completion 到 sidecar，再处理 Agent ACK、取消屏障、
  未解析终态与 scheduler 通知。
- `api/routes.py`：委派取消 HTTP handler、stale cleanup hook，以及 session lock 内一次保存的
  wakeup admission；async worker 等待 `server_turn_started` 发布后才执行；按 session SSE
  仍保留 run-journal 行为并发送首个后台任务快照。
- `server.py`：sidecar 恢复完成后先恢复/启动 inbox scheduler，再启动 Agent completion drain。

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
生成 references wire。该模块还在 stream 起始处以持久化 Manifest 为基线，并在出站前生成完整、已去重的
顶层 `artifacts` 与 `references` 快照；`api/streaming.py` 与 `api/gateway_chat.py` 仅保留该共享投影的
调用、hook、保存和出站过滤接缝，不解析 MCP JSON；前端直接替换两个快照，不重复解析或合并。

顶层 `artifacts` 是会话级累计 SSE 快照，不携带当前 turn 所有权。流结束结算只允许读取
`turns[]` 中 `turn_key` 精确匹配的行，并在 normal/error/cancel 路径合并 durable transcript 的同轮
证据。历史污染重建由 `integration/session_manifest/repair.py` 的显式维护入口负责；GET 与 SSE 均不触发
自动修复。

公开字段、SSE 帧和生命周期只在 [Session Manifest HTTP/SSE 契约](api/session-manifest-api.md)
维护；本节只记录 Fork 接缝与实现边界。
