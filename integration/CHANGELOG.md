# Integration layer changelog

Fork 特有变更（SkillHub、profiles enrich、Swagger 等）记在此文件。**不要**为集成开发去改仓库根目录的 `CHANGELOG.md`，除非用户明确要求或该变更将合并进上游正式发布说明。

格式可参考根目录 `CHANGELOG.md` 的 `[Unreleased]` 小节。

## [Unreleased]

### Added

- **Session status and unread cursors** — `GET /api/sessions` 在 integration 开启时为每行返回 `status`（`error` / `in_progress` / `has_new_messages` / `ready`）与独立 `is_unread`；`POST /api/integration/sessions/mark_read` 由服务端推进当前 Profile 会话的已读游标。游标集中存于 `{HERMES_WEBUI_STATE_DIR}/session_status.db`，以 `(profile, session_id)` 隔离；运行与异常事实仍由 Session sidecar 维护，不持久化派生 status。

- **All-profile Gateway startup** — `server.py` 默认异步确保所有可见 Profile 的 Hermes Gateway 已运行，使各 Profile 的 Cron 在 WebUI 启动后自动恢复。命名 Profile 使用独立 Hermes service，已运行实例会跳过；default 开启 `gateway.multiplex_profiles` 时只启动 default；单 Profile 失败不阻塞 WebUI。可用 `HERMES_WEBUI_START_PROFILE_GATEWAYS=0` 关闭。Gateway lifecycle 统一解析并验证 Agent 自身 launcher/venv；对于直接从已发现 Agent 源码根目录或 WebUI 仓库根目录执行 `python -m hermes_cli.main` 的部署，也会在依赖导入与 `--version` 均通过后复用当前 Python 和已验证工作目录。所有探测仍清除 `PYTHONPATH` / `PYTHONHOME`，不会把 Agent 源码注入任意 Python。Gateway runtime resolver 已收口至 `integration/gateway_startup/runtime.py`，WebUI 根目录按模块位置确定、Agent 根目录依次由 `HERMES_WEBUI_AGENT_DIR`、`${HERMES_HOME}/hermes-agent` 与既有自动发现确定。普通 Docker 容器中 `gateway start` 是成功退出但不启动进程的 no-op，新增 `integration/gateway_startup/run_container_services.sh` 作为纯 Profile Gateway supervisor：通过 Agent Profile registry 动态执行各 Profile 的 `gateway run`、遵守 multiplex、写入独立日志、转发退出信号，并在任一受管 Gateway 退出时失败退出；脚本不定位或启动 WebUI。WebUI 会自动识别无 s6 的普通容器并跳过自身 `gateway start` 协调器，外层启动器无需新增环境变量，只需分别启动该脚本与 `server.py`；s6 容器和原生 service manager 行为保持不变，显式 `HERMES_WEBUI_START_PROFILE_GATEWAYS=0/1` 仍可覆盖自动判断。WebUI 与 Agent 可直接使用 `${HERMES_HOME}/hermes-webui` / `${HERMES_HOME}/hermes-agent` 实体目录，无需 `/app` 或软链接。

- **Direct `server.py` runtime log persistence** — 直接运行 `python server.py` 时，stdout/stderr 会同时输出到终端并落盘到 `{HERMES_WEBUI_STATE_DIR}/server-<port>.log`，主日志按大小轮转（默认 10 MiB，保留 5 份）。`faulthandler` / crash visibility 使用独立 `{HERMES_WEBUI_STATE_DIR}/server-<port>-crash.log`，避免主日志轮转影响 native crash 诊断。`bootstrap.py` 会显式设置 `HERMES_WEBUI_SERVER_LOG_EXTERNAL=1`，继续只使用既有 `bootstrap-<port>.log`，不重复写 `server-<port>.log`。配置见 `integration/README.md`。

- **x_frontend Nginx reverse proxy** — `integration/frontend/nginx-x-frontend.conf` serves `x_frontend/dist` on `:8080` and proxies `/api/*` (SSE-safe) to WebUI `:8787`. No WebUI backend code changes. Local skip-login is nginx-only: JS `sub_filter` disables Casdoor redirect (`192.168.1.139:23008`), seeds `app_auth_session`, and mocks `webui_login` / `webui_logout`. See `integration/frontend/README.md`.

- **Record scripts API** — 新增 `integration/record_scripts/` 本地存储接口：脚本 JSON 字符串 `save/list/update/delete` 与关联 CSV `upload/download/delete`。`relate_name` 由前端生成并作为稳定主键；文件落盘到 `{HERMES_WEBUI_STATE_DIR}/attachments/record_scripts/<relate_name>/`，脚本删除仅删除 `script.json`，不会清理关联 CSV。Swagger 与 `integration/README.md` 同步更新。

- **Structured API error logging** — `j()` / `bad()` responses with `status >= 400` emit `[webui]` JSON `event=api_error` lines (method, path without query, status, error/message, optional traceback for 5xx). Unhandled exceptions in `server.py` use the same format (`source=unhandled`). Access logs may include `error_summary` when an API error was recorded. Module: `integration/request_logging/`. Env: `HERMES_WEBUI_API_ERROR_LOG` (default on), `HERMES_WEBUI_API_ERROR_LOG_MIN_STATUS` (default `400`). Errors are written only via `[webui]` stderr JSON (no duplicate `logging` mirror line).

- **Stream diagnostics logging** — 新增 `HERMES_WEBUI_STREAM_DIAG` 控制的流式执行诊断日志，记录 agent 初始化、上下文准备、运行耗时、最终保存和 worker cleanup summary，便于排查慢流、失败流和资源清理问题。

### Changed

- **成果库同步知识库取消总量限制** — `POST /api/integration/knowledge_base/upload_artifacts` 不再限制单次同步的全部文件总大小；单文件 50 MiB 和最多 20 个文件的限制保持不变。

- **Cron execution history is database-primary** — `GET /api/crons/history` now lists each job's `source=cron` sessions from its execution Profile `state.db`, so completed runs remain visible even without a Markdown output artifact. Existing `cron/output/<job_id>/*.md` files are attached as optional output metadata, while unmatched files remain artifact-only rows. Legacy non-default-Profile jobs with an empty stored `profile` now use the requested owner Profile for database lookup instead of incorrectly reading default.
- **Profile list response minimization** — `GET /api/profiles` no longer returns `skills`, `skill_count`, `enabled_skills`, `total_skills`, or `memory_snapshot`. Profile UI keeps runtime and `info.json` metadata only; Cron Hub now loads a selected Profile's skills on demand through `GET /api/skills?profile=<name>`.

- **Session manifest artifact authority** — `/api/session` no longer exposes `turn_artifacts`; `/api/session/manifest` is the single turn artifact display source and returns minimal `manifest_source`. New turn artifacts write only to `session_manifest.db`; legacy session JSON `turn_artifacts` is read only when the DB has no existing decision for that session/lineage, then backfilled into the DB.

- **Knowledge base upload_docs raw passthrough** — `POST /api/integration/knowledge_base/upload_docs` now forwards the incoming multipart body and `Content-Type` to downstream `upload_docs` unchanged. WebUI no longer parses/rebuilds multipart (fixes multi-file uploads where duplicate `files` parts were dropped), does not validate form fields locally, and does not inject `chunkSize`/`chunkOverlap` defaults or a WebUI-side upload size cap. Transport errors only: invalid `Content-Length`, incomplete body, downstream unreachable (502).

### Added

- **`GET /api/crons?profile=`** — Optional single-profile query lists jobs from that profile's `cron/jobs.json` without switching the WebUI active profile. Unknown profile names return 400 (`Unknown profile: …`). Same `?profile=` semantics as `/api/crons/history` and `/output`.

- **Chat apperror `content_filtered` type** — Provider 内容审核拦截（如 `data_inspection_failed`、`content_filter`、`content_policy_violation`、`moderation`）不再落到通用 `error` 兜底文案，新增 `content_filtered` 分类，中文文案「内容被审核拦截 / 输入内容被模型服务的内容审核策略拦截」，`details_label` 为「审核详情」。分类与文案集中在 `integration/chat_provider_errors/`（`classify.py`、`messages.py`），`api/streaming.py` 接缝不动。分类顺序：`content_filtered` / `compression_exhausted` 等 provider 专有 code 优先于 `404`/`401`/`429` 弱状态码匹配，避免 chatcmpl ID 子串误判（见下条 Fixed）。

### Fixed

- **Cron 会话手动续聊 Artifact** — 在已 materialize 的定时任务会话中通过 WebUI 继续对话时，成功 `write_file` 的 Artifact decision 现在会被明确验证后再标记 turn 完成；提取或 SQLite store 写入失败会记录可诊断事件，不再被静默误判为无成果或完成。

- **Cron 同会话继续对话与历史稳定性** — materialized cron session 继续沿用原 `cron_*` ID；打开或继续聊天前会从执行 Profile 的 `state.db` 仅追加补齐执行完成前的原始 cron transcript，避免后续写回丢失早先 assistant 回复，也避免把同 ID WebUI follow-up 回放再次追加成重复轮次。Cron history 同时只读取 `sessions` 行内的执行字段，不再从会随继续聊天增长的 `messages` 推导 preview 或 last activity；output artifact 只按执行结束/开始时间关联。

- **Cron 异常消息可见性** — 定时任务的失败 output 现在会在 materialized session 中补充与普通会话相同结构的 assistant 错误消息（含中文错误文案、技术详情和 `last_error_at`），因此打开 `/api/session` 可以直接看到异常；重复读取 history 不会重复写入，正常 run 不受影响。

- **Cron Manifest turn 序号与 artifact 归属** — Hermes Agent 达到工具迭代上限时写入的内部总结请求不再被 materialized cron 会话识别为新的 user turn，避免出现 `turn:47`、`turn:224` 等按消息索引生成的伪 turn。新 cron 会话只为真实请求保存连续稳定 key；sidecar 先于 artifact decision 持久化，GET Manifest 复用同一 cron-only 规范化视图，使顶层与 per-turn artifacts 对齐。继续同一 `cron_*` 会话前会在创建 pending/journal/SSE/worker 前执行 fail-closed prepare gate，确保 prefix 已保存且 follow-up 的 transcript、SSE 与 artifact decision 共用下一稳定 key；普通 WebUI 会话和历史非空 cron decisions 不变。
- **聊天定时任务 Profile 归属** — 命名 Profile 的 WebUI 会话通过 `cronjob` 工具创建任务时，现在仅在单次工具调用边界绑定该会话的 Hermes home，任务会写入对应 Profile 的 `cron/jobs.json`，不再误落到 `default`；调用结束后立即恢复 cron 路径缓存，避免并发 Profile 串写。
- **Cron Hub 手动运行动态推理配置** — `POST /api/integration/crons/run` 对未固定任务注入当前 Profile 的模型与 Provider，并清除本次执行副本的创建时推理快照，使手动运行明确接受当前配置而不触发漂移保护；Profile 未配置模型时，按该 Profile 的 `/api/models` 目录顺序选取首个模型；命名 Profile 仍无候选时回退到 root/default Profile 的当前推理配置或目录首项。所有变更仅作用于执行副本，不写入 `jobs.json`。原生 `/api/crons/run` 和自动 scheduler 保持不变；发现为空或失败时沿用 Agent 的异步失败记录。
- **Chat apperror HTTP 状态码子串误判** — `classify_provider_error` 此前用 `'404' in err_str` / `'401' in err_str` / `'429' in err_str` 纯子串匹配，会命中 chatcmpl/UUID 里的随机数字（如 `chatcmpl-95c64d9f-8364-4bd4-a89e-d06404ddf433` 中的 `404`），把 `data_inspection_failed` 的 400 错误误分类为 `model_not_found`。新增 `_has_http_status()` 用正则要求 3 位状态码前后有定界符（`HTTP ` 前缀 / 空格 / 冒号 / 行首），不匹配 UUID 子串。同时调整 return 顺序：`quota_exhausted` → `compression_exhausted` → `content_filtered` → `rate_limit` → `auth_mismatch` → `model_not_found`，provider 专有 code 优先于弱状态码/文本匹配。回归测试覆盖含 `404`/`401`/`429` 的 chatcmpl ID 与真实 `HTTP 404`/`401`/`429` 两类。
- **Knowledge base get_joinkb_applications passthrough** — `POST /api/integration/knowledge_base/get_joinkb_applications` proxies downstream `POST /knowledge_base/get_joinkb_applications` verbatim (no field validation). Typical body: `userId`, `uuid`, `kbName`.
- **Knowledge base mark_message_read passthrough** — `POST /api/integration/knowledge_base/mark_message_read` proxies downstream `POST /knowledge_base/mark_message_read` verbatim (no field validation). Typical body: `messageId` (integer array).
- **Knowledge base remove_from_myshkb passthrough** — `POST /api/integration/knowledge_base/remove_from_myshkb` proxies downstream `POST /knowledge_base/remove_from_myshkb` verbatim (no field validation). Typical body: `account`, `uuid`, `kbName`.
- **Knowledge base user_exit_shkb passthrough** — `POST /api/integration/knowledge_base/user_exit_shkb` proxies downstream `POST /knowledge_base/user_exit_shkb` verbatim (no field validation). Typical body: `account`, `uuid`, `kbName`.
- **Knowledge base delete_readed_message passthrough** — `POST /api/integration/knowledge_base/delete_readed_message` proxies downstream `POST /knowledge_base/delete_readed_message` verbatim. Typical body: `messageId` (integer array).
- **Knowledge base download_doc passthrough** — `POST /api/integration/knowledge_base/download_doc` proxies downstream `POST /knowledge_base/download_doc` verbatim (no field validation). Typical body: `knowledge_base_name`, `file_name`. Binary file passthrough when upstream returns non-JSON content. Document list filtering continues to use existing `POST /api/integration/knowledge_base/documents` (same downstream endpoint).

### Fixed

- **Zhiling split WebUI CSRF** — When `HERMES_INTEGRATION=1`, requests from shared frontend origins (`192.168.1.139:23003`, `47.93.211.132:23003`) with `Authorization` or `X-Forwarded-User` (auth-proxy trust boundary) bypass built-in same-origin CSRF rejection. Hook in `integration/auth/csrf_hooks.py`; `server.py` calls `install_zhiling_split_webui_csrf_hook()` at import.
- **HTTP/1.1 keep-alive auth reject body drain** — `Handler._drain_request_body()` consumes unread POST body before early auth failure returns, preventing keep-alive socket corruption and spurious 501 on the next request.

- **SkillHub custom scope empty list** — `scan_custom_skills_global` 误将上游 `hub_names`（set）当作 `_scan_custom_skill_dicts` 的 `category` 参数，SkillHub 目录非空时 `scope=custom` 列表与 `stats.custom` 恒为 0。现固定扫描 `shared_skills_dir()` 且不按 category 预过滤。

- **Notification status read mapping** — 列表项 `status` 由 `read_type` 推导：`unread`/`seen` 恒为对应值；`all` 时额外拉取下游 `readType=seen` 的 ID 集合判断 `read`/`unread`。
- **Notification delete uses delete_readed_message** — `POST /api/integration/notifications/delete` 批量转发下游 `delete_readed_message`，不再走 `creater_handle_application`（`action: delete`）。
- **Notification read uses mark_message_read** — `POST /api/integration/notifications/read` 将 `kb:` 前缀 ID 批量转发到下游 `mark_message_read`（`messageId` 整数数组），不再逐条调用 `creater_handle_application`（`action: read`）。

- **Notification read/delete request body** — `POST /read` 与 `POST /delete` 仅接受 `ids`（`string[]`）；移除单条 `id` 字段别名。单条操作传 `ids: ["kb:…"]`。

- **Notification list excludes apply-result pending** — `GET /api/integration/notifications` 与 `/summary` 永久排除 `massType=3` 且 `state=2` 的申请人「结果待处理」消息；其余类型照常返回。实现：`integration/notifications/filters.py`（`exclude_apply_result_pending`）。

- **Interrupted-turn user-visible copy (zh)** — 会话中断恢复 marker、SSE 断连提示、压缩后无响应错误、run journal 恢复控制消息改为中文。文案集中在 `integration/chat_provider_errors/interruption_copy.py`；`api/models.py` / `api/run_journal.py` 仅薄 import。`static/messages.js` 与 `static/ui.js` 同步更新。

- **Notification action_status state mapping** — 下游 `state` 与 `action_status` 对齐文档 §6.1：`0`→`rejected`、`1`→`approved`、`2`→`pending`（此前错误映射为 `0`→`pending` 等）。
- **Notification API massType documentation** — [`docs/integration-notifications-api.md`](../docs/integration-notifications-api.md) 补充下游 `massType` 四种类型（`1` 加入申请 / `2` 退出 / `3` 申请结果 / `4` 被踢出）及与 `actionable`、`action_status` 的对应关系。

- **Workspace artifact profile backfill on startup** — 服务启动时后台扫描补全 `session_manifest.db` 的 artifact profile。先跑 B 类：对 store 中无 artifact 记录、且 `session.profile` 非空的老会话（在流式落库特性 `_persist_turn_artifact_paths` 上线前创建），逐 turn 复用同一套提取逻辑（`_extract_turn_artifact_entries`）从 messages 抽取 path/source_tool/preview 并 upsert，path 格式与 turn_key 与流式落库一致，避免与 `/api/session/manifest` 的懒回填（`backfill_from_session_turn_artifacts`）冲突。再跑 A 类：修补 DB 已有记录中 `profile=''` 但 session 实际有 profile 的行。session 本身无 profile 的记录保持空（不从其他字段推断）。流式写入仍以 `session.profile` 为权威。
- **Notification actionable by massType** — `actionable` 改为按下游 `massType` 判断：`1`（入群申请）为 `1`（可打开详情，含已审批历史）；`2`/`3` 等通知类为 `0`。`action_status` 与审批按钮仅对 `massType=1` 映射。
- **Notification summary simplified** — `GET /api/integration/notifications/summary` 移除 `recent_limit` 参数与 `recent_unread` 预览，仅返回 `total` / `unread` / `by_category` 计数。
- **Notification actions field removed** — 列表响应移除 `actions[]`；按钮由前端按 `actionable` / `action_status` / `metadata.massType` 推导。

### Added

- **Browser preview for terminal agent-browser** — `browser_preview` SSE 现也在 `terminal` 工具执行 `agent-browser` CLI（含 `connect` / `snapshot` / `click` 等）时触发，与 `browser_*` 工具共用每 stream 一次性 `BrowserPreviewEmitter` 去重，不重复打开 VNC 面板。实现：`api/browser_preview.py`；`api/streaming.py` / `api/gateway_chat.py` 传入 `tool_args`。文档：`docs/browser-preview-sse.md`。

- **Notification API documentation** — [`docs/integration-notifications-api.md`](../docs/integration-notifications-api.md)：知识库通知接口说明（参数、响应字段、curl 示例、KB BFF 审批对照）。

- **Notification Phase 1 (KB-only)** — 通知 HTTP 层收窄为仅知识库 `kb_apply`：
  - 列表/摘要新增 `read_type`、`action_status` 查询；响应新增 `actions[]`、`actionable`、字符串 `action_status`
  - 下游 `state` 映射：`0=rejected, 1=approved, 2=pending`（`integration/notifications/constants.py`，见 `docs/integration-notifications-api.md` §6.1）
  - 新增 `normalize.py`、`filters.py`；handlers 不再读写 `store`
  - 删除 `GET /api/integration/notifications/{id}`；read/delete 仅处理 `kb:` ID
  - 审批仍走 `POST /api/integration/knowledge_base/creater_handle_application`
  - `store.py` / `notifications.db` 表结构**保留**作未来本地通知预留
  - Swagger、README、handlers 测试（11 用例）同步更新

- **Notification system** — `integration/notifications/` 通知模块（见上文 Phase 1）。`integration/tests/notifications/test_store.py` 保留 store 单测（7 用例）。

### Fixed

- **SkillHub installed detection for long catalog names** — Hub catalog `name` 可超过 64 字符，但安装目录 leaf 经 `normalize_dir_name` 截断；`annotate_installed` 用完整 catalog `name` 查索引导致 `installed: false` 与安装 409 矛盾。修复：安装时写入 `.hub_catalog_name` sidecar；索引与列表查找对截断 leaf 做 fallback。
- **Notification KB user identity passthrough** — `GET /api/integration/notifications` 和 `GET /api/integration/notifications/summary` 调用下游 `get_user_messages` 时缺少 `account`/`uuid` 参数，导致知识库消息无法按用户过滤。修复：从请求 query params 读取 `account`/`uuid` 并透传到下游（与知识库 BFF 的调用方传参模式一致）。
- **Notification read/delete KB forwarding** — `POST /api/integration/notifications/read` 和 `POST /api/integration/notifications/delete` 对 `kb:` 前缀的知识库通知此前只留了 TODO，未实际转发下游。修复：从请求 body 读取 `account`/`uuid`，将 `kb:` 前缀 ID 剥离后逐个转发到下游 `creater_handle_application`（`action` 分别为 `read`/`delete`）。Swagger 同步补充 4 个接口的 `account`/`uuid` 参数文档。新增 handlers 测试：`integration/tests/notifications/test_handlers.py`（8 个用例）。
- **Notification response Content-Length** — 通知 handlers 手动写响应未设置 `Content-Length`，HTTP/1.1 keep-alive 下 curl/浏览器会挂起约 30s 直到超时。改为统一使用 `api.helpers.j()` 写 JSON 响应。
- **Notification KB field mapping** — 下游 `get_user_messages` 必填 `readType`；消息字段为 `massage`/`createTime`/`showName`/`state`，非此前假设的 `messages`/`createdAt`/`kbName`。列表请求补 `readType: all`，归一化层按下游实际字段映射。

- **Knowledge base upload_artifacts** — `POST /api/integration/knowledge_base/upload_artifacts` orchestrates workspace artifact upload to the knowledge base. Parameters align with `upload_docs`: `uuid`, `kbName`, `fileProperties` (passthrough, `fileClass` = 直属库类型), `chunkSize`, `chunkOverlap`, plus new `paths` array (workspace-relative paths, parallel to `fileProperties`). WebUI reads workspace file bytes, forwards multipart to downstream `upload_docs` without constructing or mutating `fileProperties`. 单文件 50 MiB / 最多 20 个文件，不限制单次同步文件总大小。Path traversal blocked by `safe_resolve_ws`. Only performs `upload_docs`; `update_docs` remains a separate caller responsibility.

- **Knowledge base passthrough routes** — `POST /api/integration/knowledge_base/creater_handle_application` and `POST /api/integration/knowledge_base/get_user_messages` proxy downstream `creater_handle_application` (approve/reject/ignore join requests) and `get_user_messages` (user notification list). Request and response bodies are forwarded verbatim with no field validation or payload building. New `PASSTHROUGH_ROUTES` set in `integration/knowledge_base/constants.py` marks routes that skip the `_ROUTE_BUILDERS`/`_REQUIRED_FIELDS` machinery in `handlers.py`.

- **Cron session turn_artifacts persistence** — cron sessions now have `turn_artifacts` persisted to the sidecar JSON and `session_manifest_records` SQLite table, mirroring the WebUI streaming pipeline (`_persist_turn_artifact_paths`). Previously cron sessions left `turn_artifacts` empty, so the manifest relied on re-extracting artifacts from messages every request — fragile and lost entirely after conversation compression. After each cron run materializes the sidecar, `_persist_cron_turn_artifacts` stamps stable `_turn_key`s on user messages (state.db messages don't carry them) and calls `_persist_turn_artifact_paths` per turn.

- **Cron session materialization unconditional** — cron session sidecar creation (materialize from `state.db`) no longer requires `HERMES_INTEGRATION=1`. The materialize hook (`_install_run_job_materialize_hook`) installs unconditionally in `install_cron_integration_hooks()`, so cron runs always get a WebUI sidecar when `state.db` data exists. This fixes `/api/session/manifest?session_id=cron_*` returning 404 and `turn_artifacts` being empty when integration mode was off. The preserve-once cron hook (keeping repeat-limited jobs as completed/disabled instead of deleting) remains gated on `HERMES_INTEGRATION=1` since it alters cron job lifecycle behavior only relevant to the Cron Hub UI.

- **SkillHub local_all scope** — `GET /api/skillhub/skills?scope=local_all` aggregates installed hub skills and local self-built custom skills into a single list. Custom wins on duplicate `name` (hub installed item dropped). Reuses the same `category`/`q`/`sort`/`order`/`page`/`page_size`/`all` query parameters and the unified response envelope; `stats` keeps the original four fields (`hub`/`installed`/`not_installed`/`custom`).

- **Skill no-self-improve lock** — `skills.no_self_improve` in active profile `config.yaml` blocks agent self-evolution for listed skills (enforced by Hermes Agent `skill-policy` plugin). `GET/POST/PUT /api/skillhub/skills/no_self_improve*` for config read, custom toggle, and bulk replace (requires `HERMES_INTEGRATION=1` only). Hub-installed skills (`.hub_installed`) are permanently locked: startup sync reconciles hub names and removes stale entries; install/delete hooks update config. Skill list APIs add `no_self_improve` and `can_lock`; Skills and SkillHub panels show lock icons (custom toggleable, hub read-only).

- **SkillHub list all mode** — `GET /api/skillhub/skills` accepts `all=1` (only the literal `1`) to return the full filtered list for any `scope`/`category`/`q`/`sort`/`order` without pagination. Response keeps the same envelope with `page=1` and `page_size=total`. Other `all` values (including `true`) use default pagination.

- **Integration workspace file delete** — `POST /api/integration/workspace/file/delete` removes files under `HERMES_WEBUI_DEFAULT_WORKSPACE` (`paths` array). Left-rail Workspace files UI adds per-row delete, multi-select batch delete, and manifest refresh after delete. Session manifest artifacts keep historical rows with `status: "expired"` when the workspace file is gone (references unchanged).

- **SkillHub list sorting** — `GET /api/skillhub/skills` accepts `sort` (`name`|`mtime`, default `name`) and `order` (`asc`|`desc`, default `asc`). List items always include aligned fields (`display_name`, `category`, `mtime`, etc.); strings default to `""`, `mtime` defaults to `null`. Hub upstream `updated_at` is normalized into `mtime`. SkillHub sidebar adds a sort dropdown; removes client-side re-sort of the current page.

- **Knowledge base search_docs_xcore** — `POST /api/integration/knowledge_base/search_docs_xcore` proxies downstream `POST /knowledge_base/search_docs_xcore` with `query`, `kbNames` (→ downstream `kbNames`), optional `topK` (default 3), and `scoreThreshold` (default 1.0). Success returns `{kbNames, docNames, context}` from upstream `data`.

- **Knowledge base search_docs** — `POST /api/integration/knowledge_base/search_docs` proxies downstream `POST /knowledge_base/search_docs` with `query`, `kbName` (→ `knowledge_base_name`), optional `topK` (default 3), and `scoreThreshold` (default 1.0). Returns matched document chunks as JSON array.

- **Knowledge base show_pdf binary passthrough** — `show_pdf` proxies downstream PDF bytes (`application/pdf`) instead of requiring JSON; JSON errors still mapped as before.

- **Knowledge base show_pdf** — `POST /api/integration/knowledge_base/show_pdf` proxies downstream `POST /knowledge_base/show_pdf` with `kbName`, `fileName`, and optional `flag`.

- **Profile pin** — `POST /api/profile/pin` (`name`, `pinned`) stores pin state in each profile's `info.json` (`pinned`, `pin_order`), including `default`. New pins get `pin_order: 1` (topmost) and bump existing pinned orders. `GET /api/profiles` returns `info.pinned` / `info.pin_order` and sorts pinned (by `pin_order`) → unpinned `default` → alphabetical (max 5 pins). Profiles panel and compose dropdown UI in `hermes_profiles.js`.

- **Zhiling identity lookup API doc** — [`docs/integration-login-api.md`](../docs/integration-login-api.md) documents `GET /api/integration/webui_login` request/response contract, auth, and error semantics.

### Changed

- **Knowledge base PDF preview timeout** — `POST /api/integration/knowledge_base/show_pdf` now allows 180 seconds for the downstream request; other knowledge-base proxy routes retain their existing timeout behavior.

- **SkillHub list performance (Phase 1)** — `GET /api/skillhub/skills` deduplicates work within each request: one upstream hub catalog fetch, one custom local scan, and one install-index/config read for annotate. `scope=installed|custom` with `all=1` benefits most; response shape and stats semantics unchanged.

- **SkillHub installed list description** — `GET /api/skillhub/skills` (`scope=installed` and other hub-catalog scopes) prefers each installed skill's `description` from local `SKILL.md` frontmatter under `shared_skills_dir`; when local description is absent, upstream catalog value is kept.

- **SkillHub preview `scope=auto`** — `GET /api/skillhub/content|structure|file` with `scope=auto` (default) reads only from `{HERMES_HOME}/skills`; missing local `SKILL.md` returns 404 (no SkillHub fallback). `scope=hub` keeps local-first then upstream; `scope=custom` remains local-only. SkillHub panel passes `scope=hub` for catalog items not yet installed.

- **SkillHub preview `scope=hub`** — `GET /api/skillhub/content|structure|file` with `scope=hub` resolves `{HERMES_HOME}/skills` first, then falls back to SkillHub upstream when no local `SKILL.md` exists. `scope=custom` remains local-only.

- **Knowledge base BFF route segment names** — WebUI proxy paths `apply-join`, `upload-docs`, `update-docs`, and `delete-docs` are now `apply_join`, `upload_docs`, `update_docs`, and `delete_docs` (underscore only). Hyphenated segments are no longer served. Swagger and [`docs/integration-knowledge-base-api.md`](../docs/integration-knowledge-base-api.md) updated.

- **Zhiling login/logout API paths** — `GET /api/integration/login` → `GET /api/integration/webui_login`; `POST|GET /api/integration/logout` → `POST|GET /api/integration/webui_logout`. Swagger, docs, and tests updated.

- **Zhiling identity in-process cache** — `GET /api/integration/webui_login` now caches successful Control Plane identity lookups in memory. Requests with `Authorization: Bearer` refresh the cache; requests without Bearer return the cached identity JSON (no `access_token` in the response). Cache misses return `401` + `not_registered`; expired cache returns `401` + `session_expired`. TTL defaults to JWT `exp` when present, otherwise `ZHILING_IDENTITY_CACHE_TTL_SECONDS` (default 1800). `POST /api/integration/webui_logout` clears the cache. Process restart clears the cache.

- **`GET /api/integration/webui_login` response timestamp** — All JSON responses include `timestamp` (Unix seconds) indicating when the server generated the response.

- **Cron sessions in `/api/sessions`** — Cron execution sessions (`source_tag: cron` / `cron_*` ids) are removed from `GET /api/sessions` responses when integration is enabled, including `?all_profiles=1` and profile pagination. WebUI setup chats (`source_tag: webui`) are unchanged.

- **Chat stream Chinese apperror UX** — Provider/SSE `apperror` classification, Chinese copy, payload shaping, and persisted error messages live in `integration/chat_provider_errors/` (`messages.py`, `classify.py`, `payload.py`). `api/streaming.py` re-exports thin aliases for existing call sites. User-visible `message`/`hint` come from `CHAT_ERROR_ZH` only; agent/provider raw text is redacted into SSE `details` / persisted `provider_details` (collapsible technical block), not appended to `message`.

- **SkillHub hub list pagination** — `scope=hub` now loads the full upstream catalog locally (same as installed filters), applies local `q` substring search, sorts, then paginates. Upstream `q`/page params are no longer used for hub list.

- **`GET /api/media` session-relative paths** — When `path` is relative and `session_id` is set, resolution matches `GET /api/file/raw` (session workspace, then attachment inbox) with the same inline/disposition rules. Absolute `path` behavior is unchanged. `/api/file/raw` unchanged for existing clients. Swagger updated.

- **Knowledge base BFF passthrough responses** — JSON proxy routes now return downstream HTTP status and body unchanged (no `data` unwrapping or business-error remapping). `show_pdf` binary responses still passthrough bytes; upload-docs no longer maps `data: null` to `{"ok": true}`.

- **MCP 传输类型** — 设置面板添加 MCP 服务器时支持三种传输：`stdio`、HTTP (Streamable)、SSE。SSE 保存为 `transport: sse`；HTTP Streamable 仅写 `url`/`headers`。同步更新 `PUT /api/mcp/servers/{name}` 列表摘要与 integration Swagger。

- **MCP 连通性测试** — `POST /api/mcp/servers/{name}/test` 现真正连接目标服务器（含 SSE）并返回 `tool_count`；探测为临时连接，不修改长期 MCP registry。

- **Knowledge base BFF route prefix** — WebUI proxy paths use `/api/integration/knowledge_base/*` (underscore), aligned with downstream `/knowledge_base/*`. Hyphenated `/api/integration/knowledge-base/*` is no longer served.

- **Integration workspace files profile (restored via DB)** — `GET /api/integration/workspace/files` 恢复 `?profile=` 过滤与 `profile` 标注，改由直查 `session_manifest.db`（`api/session_manifest_store.py` 新增 `get_artifact_profile_index` / `get_artifact_paths_for_profile`）实现，取代旧的 `artifact_profiles.py` 跨会话索引（已删除）。传入 `profile` 时仅返回该 profile 的 manifest 成果文件（走 stat-only 快路径）；不传则返回全部文件并对成果附加 `profile`。session-save hook 不再增量维护 profile 索引——store 在流式 turn 结束时写入，为权威来源。

- **Integration workspace files performance** — `GET /api/integration/workspace/files` keeps an in-memory workspace file index (invalidated on session save and optional `refresh=1`); pagination/filter/sort reuse the cached index instead of re-walking the tree on every request. Collection uses `os.scandir`; `?profile=` stat-only fast path skips full walk. Artifact profile index skips unrelated sessions by workspace, merges incrementally on session save, and left-rail UI reloads on SSE `manifest_delta` file artifacts. Set `HERMES_DEBUG_TIMING=1` for `X-Hermes-Timing-*` response headers.

- **SkillHub download sidecar filename** — Zip sidecar renamed from `.hermes-skill-origin.json` to `.skill-origin.json` (`GET /api/skillhub/download` / upload round-trip).

- **SkillHub upload dedup / round-trip** — `POST /api/skillhub/upload` resolves target path via optional `dir_name`, zip sidecar `.skill-origin.json`, existing custom dirs with the same frontmatter `name`, then `category`+leaf. `overwrite=true` removes all custom copies with that `name` before write (hub-installed still 409). Single `.md` uploads use frontmatter `name` instead of the `SKILL.md` filename stem. `GET /api/skillhub/download` emits `{leaf}/…` zip paths plus sidecar metadata. Custom list dedupes by `dir_name`; upload UI opens the imported row by `dir_name`.

- **Session manifest turn reconcile** — At SSE `turn_complete` (`source.tool: reconcile`, before `done`) and in `GET /api/session/manifest`, supplement whitelist artifact extraction with per-turn transcript mining (tool args/result/diff, `MEDIA:`, and assistant delivery prose such as `文件位置:` paths). Candidate paths must pass workspace `_file_preview_path` (real file exists); regex hits without a file on disk are dropped. Prose paths use `source_tool: assistant_prose`. Dedupes by path; write-tool `source_tool` wins over `media` / `assistant_prose` on the same path.

### Added

- **Manifest artifact profile** — `GET /api/session/manifest` and SSE `manifest_delta` include optional `profile` on `artifacts[]` rows (from `session.profile`). `GET /api/integration/workspace/files` annotates manifest file artifacts with `profile`; optional `?profile=` returns only that profile's artifacts (default still lists all workspace files). 跨会话索引现由直查 `session_manifest.db`（`api/session_manifest_store.py`）实现。

- **Session workspace inspector** — `GET /api/session/manifest` returns structured todos, artifacts, and referenced files parsed from tool activity; the right panel adds **Tasks**, **Artifacts**, and **Refs** tabs with file preview via the existing workspace preview path. Artifacts outside the session workspace are listed with absolute paths and file metadata, while previews remain scoped to workspace files.

- **Session manifest realtime updates** — Active chat streams emit `manifest_delta` SSE events for explicitly parsed todo, artifact, and reference tool activity. The inspector can update during tool execution, while completed and historical sessions still rebuild from `/api/session/manifest`; live and historical manifest data share canonical `turn:<user_msg_idx>` turn keys, and per-turn artifacts can be shown under the specific user turn that changed them.

- **SkillHub local skill zip download** — `GET /api/skillhub/download?name=&dir_name=` streams a zip of a skill under `shared_skills_dir` (custom or hub-installed). Excludes `.hub_installed`, `.category`, `.install_name`. Requires `HERMES_INTEGRATION=1` only. Detail UI download button in `hermes_skillhub.js`.

- **SkillHub upload overwrite** — `POST /api/skillhub/upload` accepts `overwrite` (multipart field or JSON boolean). When true, replaces existing **custom** skills only; hub-installed targets still return 409.「我的创建」upload sends `overwrite=1` by default.

- **SkillHub custom skill edit** — `POST /api/skillhub/edit` updates `SKILL.md` for existing custom skills in `shared_skills_dir` (`name`, `content`, optional `dir_name`). Hub-installed skills (`.hub_installed`) are rejected with 403. Requires `HERMES_INTEGRATION=1` only. SkillHub「我的创建」详情页提供编辑/保存/取消按钮（`hermes_skillhub.js`）。

- **Knowledge base BFF proxy** — When `HERMES_INTEGRATION=1` and `KNOWLEDGE_BASE_URL` are set, `POST /api/integration/knowledge-base/*` proxies 13 downstream `POST /knowledge_base/*` endpoints. Caller passes `account` and `uuid` in the JSON body (or multipart form for `upload-docs`); WebUI does not call Zhiling identity lookup. Success returns upstream `data` only; upload requires `upload-docs` then `update-docs`. See `integration/knowledge_base/` and Swagger tag `IntegrationKnowledgeBase`.

- **Session manifest skill artifacts** — `GET /api/session/manifest` and SSE `manifest_delta` list conversation-created/updated skills in `artifacts[]` with `preview: "skill"`: from Hermes Agent `skill_manage` (`action`: `create`, `edit`, `patch`, `write_file`) and from generic write tools (`write_file`, `edit_file`, etc.) when the target path is `{HERMES_HOME}/skills/.../SKILL.md` under the session profile. Completed `skill_manage` results may supply nested `path` (e.g. `github/github-trending`). Requires integration enabled and `SKILL.md` on disk. Preview via `GET /api/skillhub/content`. `skill_view` remains in `references[]` only.

- **Session manifest MEDIA artifacts** — `GET /api/session/manifest` and SSE `manifest_delta` (`source.kind=turn_complete`) now include assistant-delivered local files from `MEDIA:<path>` tokens in `role=assistant` messages (`source_tool: media`). Workspace-relative paths preview via integration workspace file API; workspace-external absolute paths preview via `/api/media?path=&session_id=`. Per-turn chips still refresh from authoritative GET after turn `done`. Write-tool `source_tool` wins over `media` on the same path.

### Changed

- **One-shot cron jobs retained after completion** — When `HERMES_INTEGRATION=1`, integration hooks preserve repeat-limited cron jobs in `jobs.json` as `enabled=false` / `state=completed` instead of letting Hermes Agent auto-delete them after the final run. Output history and Cron Hub listing stay available until explicit delete. Tasks panel one-shot warning copy updated accordingly.

- **One-shot cron scheduler retention and history linking** — The retention hook now also patches `cron.scheduler.mark_job_run`, not only `cron.jobs.mark_job_run`, so scheduled ticks that cached the function at import time do not bypass WebUI's preserve-once behavior. Cron history also maps output `.md` files to already materialized `cron_*` WebUI sessions when the original one-shot job row has already disappeared.

- **Integration workspace files panel** — Opening the Workspace files rail no longer auto-previews the last selected file; the list may still restore selection highlight, and preview loads only after an explicit file click.

- **Integration workspace `ctime_ns` fallback** — `GET /api/integration/workspace/files` entries now populate `ctime_ns` from `st_ctime_ns` when `st_birthtime` is unavailable (e.g. Linux/Docker `/workspace`). No new response fields.

- **Integration workspace files search/filter/sort** — `GET /api/integration/workspace/files` supports `q` (basename contains), `type` (file extension filter, e.g. `.md`), `sort` (`path`/`size`/`mtime`/`ctime`), and `order` (default `desc`). Response entries include `ext`, `mime`, `mtime_ns`, `ctime_ns`; response adds `total`, `has_more`, and query echo. Left-rail UI uses server-side filtering/sorting (`hermes_integration_workspace.js`).

- **Knowledge base defaults in code** — `location`（create/edit 默认 `101`）与分页默认 `size`（`15`）改为 `integration/knowledge_base/constants.py` 常量，不再通过 `KNOWLEDGE_BASE_LOCATION` / `KNOWLEDGE_BASE_DEFAULT_PAGE_SIZE` 环境变量配置。调用方仍可在请求体中显式传入覆盖。

- **Integration workspace file API unified stream** — `GET /api/integration/workspace/file` now always returns raw file bytes with MIME by extension; no JSON text response, no `inline`/`download` query params, no `Content-Disposition`. Removed `GET /api/integration/workspace/file/raw`. Manifest and integration workspace rail preview use `fetch` + client-side rendering (HTML via sandboxed `srcdoc`).

- **SkillHub preview `scope=auto` (default)** — `GET /api/skillhub/content|structure|file` without `scope` (or `scope=auto`) resolves `{HERMES_HOME}/skills` first via `has_local_skill`, then falls back to SkillHub upstream. `scope=custom` / `scope=hub` remain local-only / hub-only. Manifest and workspace skill preview benefit without passing `scope=custom`.

- **Session manifest wire format** — `GET /api/session/manifest` and SSE `manifest_delta` rows now use slim `{path, preview, source_tool}` entries only (`preview`: `"file"` | `"skill"`). Non-previewable paths are omitted. File preview from manifest uses `/api/integration/workspace/file`; skills still use `/api/skillhub/content`. Removed `/api/file/allowlisted`.

### Fixed

- **Session manifest skill preview** — Removed duplicate `renderSessionArtifacts` so the Artifacts inspector tab uses manifest rows with `preview: "skill"` and routes to `/api/skillhub/content` instead of legacy `openArtifactPath` → `/api/list`. `#workspace=` links and absolute `~/.hermes/skills/...` paths now resolve via SkillHub (`content` / `file`); workspace `/api/list` is no longer used for out-of-workspace skill paths.

- **Integration workspace cruft filter** — Flat file index and read/raw endpoints skip `.DS_Store`, `Thumbs.db`, `._*` AppleDouble files, and do not descend into `.git` / `node_modules` / `__pycache__` etc. (aligned with session workspace tree filter in `ui.js` #1793).
- **Cron session model metadata** — Materialized cron run sidecars now preserve the model recorded in the Agent `state.db` session row, and existing cron sidecars stuck on `model: "unknown"` are repaired during materialization instead of relying on the frontend's later model fallback write.
- **Cron session sidebar visibility** — `/api/sessions` now refreshes stale zero-count `cron_*` index rows from their WebUI sidecar JSON before returning the sidebar payload, so materialized cron runs with persisted fallback messages are no longer hidden by the frontend's empty-session filter.
- **Cron session delete** — `POST /api/session/delete` on materialized `cron_*` sessions now removes the run from the correct profile `state.db` (not only the active profile), deletes the matching `cron/output/<job_id>/*.md` run artifact from the job **owner** profile store (including conservative orphan cleanup when the `state.db` row is already gone but the output `.md` remains), and prevents Cron Hub `/api/crons/recent` and history materialize from resurrecting deleted cron runs.
- **Cron job delete cleanup** — `POST /api/integration/crons/delete` now removes the deleted job's materialized `cron_<job_id>_*` WebUI sessions, matching `state.db` rows across profiles, and the owner profile `cron/output/<job_id>/` history directory so deleting a task does not leave stale history behind.

### Added

- **Workspace files UI (left rail)** — When `HERMES_INTEGRATION=1`, rail/sidebar panel `integrationWorkspace` lists the global workspace index (`/api/integration/workspace/files`) with client-side path filter, paginated load-more, and read-only preview in the main area (`hermes_integration_workspace.js`). Coexists with session-scoped right-side Workspace.
- **Workspace session-less file APIs** — When `HERMES_INTEGRATION=1`, `GET /api/integration/workspace/files` (flat paginated index under `HERMES_WEBUI_DEFAULT_WORKSPACE`), `GET /api/integration/workspace/file` (text JSON), and `GET /api/integration/workspace/file/raw` (binary/inline/download). No `session_id`; root via `resolve_trusted_workspace(None)`.
- **Zhiling user-container logout** — When `HERMES_INTEGRATION=1` and `ZHILING_LOGOUT_API_URL` (auth-proxy origin only, e.g. `http://auth-proxy:8080`) are set, `POST /api/integration/logout` clears the WebUI `hermes_session` cookie, POSTs `{}` to `{ZHILING_LOGOUT_API_URL}/api/logout` (path fixed in code; no browser Cookie forwarded), and returns the upstream JSON unchanged (`casdoor_logout_url`, `login_url`, etc.; 502 on unreachable auth-proxy). `GET` on the same path returns `405 method_not_allowed`. Frontend Sign Out is unchanged; callers use this API or auth-proxy `/api/logout` directly.
- **Zhiling identity lookup** — When `HERMES_INTEGRATION=1` and `ZHILING_CONTROL_PLANE_URL` are set, `GET /api/integration/login` proxies `Authorization: Bearer <access_token>` to Control Plane `/api/identity/lookup` and returns the upstream JSON unchanged (401/403 passthrough; 502 on unreachable upstream).
- **Cron Hub unread counts** — Cron Hub now persists per-job read cursors, exposes `GET /api/integration/crons/unread` and `POST /api/integration/crons/unread/read`, and shows unread run counts on the Cron Hub rail/sidebar badge.
- **Egress policy (iptables)** — Add operator-only `/api/integration/egress/policy` API to apply `open` or `whitelist` iptables-restore rules when `HERMES_INTEGRATION=1` and `HERMES_EGRESS_POLICY_ENABLED=1` are set. Supports strict IP/CIDR validation and opt-in `include_request_ip` safeguard.
- **Cron execution status fields** — Cron job payloads now include `execution_bucket` (`running` / `waiting` / `error`) and `execution_state` (细分原因，如 `manual_running`、`scheduled_waiting`、`last_run_error`), and Cron Hub's status filter now uses the bucket field.
- **Cron Hub UI parity** — Cron Hub list/detail/forms reuse Tasks panel markup (`detail-card`, `detail-run-item`, `detail-form`) via `window.HermesCronShared` from `static/panels.js`.
- **Cron run session links** — `/api/crons/history` and `/api/crons/run` include `session_id` when a cron run can be materialized from `state.db`; Tasks and Cron Hub run history rows show an open-session action for full execution steps.
- **Cron fallback messages** — When a cron run has no `state.db` messages, materialize writes a two-message WebUI session: user = `job.prompt` only; assistant = run `.md` body (`source: cron_fallback`). Persists to sidecar JSON so follow-up chat keeps context. Does not overwrite sessions that already have messages.
- **Cross-profile cron (Cron Hub)** — `HERMES_INTEGRATION=1` enables `GET /api/crons?all_profiles=1` (grouped by profile), `GET /api/crons/recent?all_profiles=1` (completions with `profile` + optional `session_id` backfill), `profile` query on history/run/output, `POST /api/integration/crons/*` CRUD/run with explicit `profile`, session materialize after runs (`integration/crons/session_bridge.py`), and UI panel `integrationCrons` (`hermes_integration_crons.js`).
- **SkillHub upload stats** — `POST /api/skillhub/upload` 成功响应为 `skill_count` + `file_count`（ZIP 按各技能目录递归统计文件数）；移除与 `skill_count` 重复的 `count`。
- **SkillHub upload errors (zh)** — 上传失败响应 `error` 字段改为中文（含 SKILL.md 校验、ZIP 解析、multipart/JSON 参数校验）。
- **SkillHub category paths + multi-skill ZIP** — install/upload share `skill_target_dir`: with category → `skills/<category>/<leaf>/` + `.category`; without → flat `skills/<leaf>/`. Hub install reads catalog `category` (body or detail). `annotate_installed` uses `rglob(".hub_installed")` for nested installs. ZIP upload discovers multiple `SKILL.md` roots, batch copy with rollback, response `{ count, skills[] }`.
- **Custom upload format validation** — `POST /api/skillhub/upload` validates SKILL.md: YAML frontmatter (`---`), required `name` and `description`, name/path rules, optional `skill_matches_platform` when agent tools are available (`integration/skills/validate.py`).
- **SkillHub upload category** — selected category chip is sent on upload; skills land under `skills/<category>/<dir_name>/` with `.category` marker; response `dir_name` is the relative path.
- **SkillHub delete nested paths** — `dir_name` uses path relative to `skills/` (e.g. `apple/apple-notes`); delete falls back to scanning subdirectories by `name` when `dir_name` is wrong.
- **Unified local skill delete** — `POST /api/skillhub/delete` removes hub installs and custom skills from `shared_skills_dir()`; `POST /api/skillhub/uninstall` removed. UI uses one Delete action with confirm.
- **Custom skill upload** — `POST /api/skillhub/upload` writes to `shared_skills_dir()` only (no upstream). Multipart `.md`/`.zip` or JSON `content`; `dir_name` from `name` or filename stem; response includes `list_name` from SKILL.md frontmatter. UI: drag-and-drop zone + browse on SkillHub「我的创建」tab. Requires `HERMES_INTEGRATION=1` only.
- **Profile memory_snapshot** — `GET /api/profiles` (integration enabled) adds nested `memory_snapshot` per profile: `MEMORY.md`, `USER.md`, `SOUL.md` from each profile's `path`, with the same fields as `GET /api/memory` (redaction via `api_redact_enabled`). Loader: `integration/profiles/memory_snapshot.py`.
- **Profile info read/write** — `GET /api/profiles` adds nested `info` + full `skills` per profile when `HERMES_INTEGRATION=1`. `POST /api/profile/info` writes `info.json` (logo as Data URI base64). `GET /api/profile/logo-presets` lists 24 built-in logos. UI: logo picker, profile edit, create form (`hermes_profiles.js`). Script: `integration/scripts/fetch_profile_logos.py`.

### Changed

- **Cron Hub profile selection** — Cron Hub create/edit now uses a single Profile field: jobs are stored in and executed under the same Profile. Integration write APIs (`/api/integration/crons/*`) accept `profile` only in JSON bodies (reject `owner_profile`); manual runs execute under that Profile. Read APIs now use `profile` for history/run/output queries instead of the former `owner_profile` parameter.
- **Cron Hub create form** — Creating integration cron jobs requires an explicit Profile (server default is no longer selectable) and exposes a Tasks-style skill picker populated from that Profile's installed skills.
- **Cron history session IDs** — `/api/crons/history` keeps returning `session_id`, but now batch-materializes the page with one `state.db` candidate scan instead of reopening/querying SQLite once per output file.
- **Cron session selection** — `materialize_cron_session` loads all matching `state.db` cron sessions (removed erroneous `LIMIT 1`) so `run_mtime` can map a run output file to the correct session row.
- **Integration env flag** — `HERMES_INTEGRATION=1` now enables both profile/skills integration and Cron Hub, replacing the separate `HERMES_INTEGRATION_SKILLS` and `HERMES_INTEGRATION_CRON_ALL_PROFILES` flags.
- **SkillHub list item shape** — `GET /api/skillhub/skills` 的 `skills[]` 在 hub 与 custom scope 下统一包含 `dir_name`、`installed`、`hub_installed`、`custom`、`disabled`；hub 已安装项附带相对 `shared_skills_dir` 的路径，未安装项 `dir_name` 为空字符串；custom 项 `installed` 恒为 `true`。
- **Cron OpenAPI** — `integration/swagger/openapi.json` 与上游 `api/routes.py` 定时任务实现对齐：拆分 `POST /api/crons/create|update|delete|pause|resume`；`GET`/`POST` `/api/crons/run` 区分读输出与触发运行；补全 history/output/recent/status 查询参数与响应 schema。
- **SkillHub 与 profile 解耦** — install / delete / `installed` 标注均使用 `shared_skills_dir()`，不再读取 WebUI profile cookie。
- **SkillHub upload seam** — `POST /api/skillhub/upload` 经 `try_handle_post_early()` 分发（须在 `read_body` 前，支持 multipart）；JSON 类 POST 仍走 `try_handle_post()`。
- **SkillHub handler return** — `integration/skills/handlers.py` 在写响应后返回 `True`（`j()`/`bad()` 本身返回 `None`），修复 upload 等路由在 `routes.py` 的 `is True` 判断下漏拦截、二次 `read_body` 超时的问题。
- **Profile logo presets** — `fetch_profile_logos.py` now downloads real PNGs (DiceBear 9.x avatars/abstract art, Google Noto Emoji) instead of solid-color placeholders. Regenerate with network: `python3 integration/scripts/fetch_profile_logos.py`.
- **`scope=custom` skills** — 列表与 `stats.custom` 固定扫描 `{HERMES_HOME}/skills`（`shared_skills_dir()`），不再使用 Cookie profile 下的 `profiles/<name>/skills`。
- **Swagger `/docs`** — `servers` 在 `GET /api/openapi.json` 时按请求头（`Host`、`X-Forwarded-Host`、`X-Forwarded-Proto`）动态注入，Try it out 不再写死 `localhost:8787`。Swagger UI 资源改为 `integration/assets/swagger-ui/`（swagger-ui-dist@5.18.2），内网离线可用，不再依赖 jsDelivr CDN。`/docs` 使用独立 `favicon.svg`，不再回退到 WebUI 根路径 `favicon.ico`。
- **SkillHub proxy bypass** — `ensure_skillhub_no_proxy()` in `config.py` merges `SKILLHUB_URL` host into `NO_PROXY` / `no_proxy` at server startup; `.env.example` documents manual `NO_PROXY` when `HTTP_PROXY` is set.
- **SkillHub OpenAPI** — `integration/swagger/openapi.json` 与 `/api/skillhub/*` 实现对齐（`scope`、列表 envelope、cookie profile、`file` 查询别名等）。
- **SkillHub `stats`** — `GET /api/skillhub/skills` 的 `stats` 为全库全局计数（所有分类），不再随 `category` 变化；`skills`/`total` 仍按 category/q 过滤。
- **SkillHub custom 预览** — `scope=custom` 下列表项可预览 SKILL.md 与 scripts/references；`GET /api/skillhub/content|structure|file` 支持 `scope=custom`，从 `{HERMES_HOME}/skills` 读取本地自建技能。
