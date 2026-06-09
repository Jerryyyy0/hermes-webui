# Integration layer changelog

Fork 特有变更（SkillHub、profiles enrich、Swagger 等）记在此文件。**不要**为集成开发去改仓库根目录的 `CHANGELOG.md`，除非用户明确要求或该变更将合并进上游正式发布说明。

格式可参考根目录 `CHANGELOG.md` 的 `[Unreleased]` 小节。

## [Unreleased]

### Added

- **Session workspace inspector** — `GET /api/session/manifest` returns structured todos, artifacts, and referenced files parsed from tool activity; the right panel adds **Tasks**, **Artifacts**, and **Refs** tabs with file preview via the existing workspace preview path. Artifacts outside the session workspace are listed with absolute paths and file metadata, while previews remain scoped to workspace files.

- **Session manifest realtime updates** — Active chat streams emit `manifest_delta` SSE events for explicitly parsed todo, artifact, and reference tool activity. The inspector can update during tool execution, while completed and historical sessions still rebuild from `/api/session/manifest`; live and historical manifest data share canonical `turn:<user_msg_idx>` turn keys, and per-turn artifacts can be shown under the specific user turn that changed them.

- **SkillHub local skill zip download** — `GET /api/skillhub/download?name=&dir_name=` streams a zip of a skill under `shared_skills_dir` (custom or hub-installed). Excludes `.hub_installed`, `.category`, `.install_name`. Requires `HERMES_INTEGRATION=1` only. Detail UI download button in `hermes_skillhub.js`.

- **SkillHub upload overwrite** — `POST /api/skillhub/upload` accepts `overwrite` (multipart field or JSON boolean). When true, replaces existing **custom** skills only; hub-installed targets still return 409.「我的创建」upload sends `overwrite=1` by default.

- **SkillHub custom skill edit** — `POST /api/skillhub/edit` updates `SKILL.md` for existing custom skills in `shared_skills_dir` (`name`, `content`, optional `dir_name`). Hub-installed skills (`.hub_installed`) are rejected with 403. Requires `HERMES_INTEGRATION=1` only. SkillHub「我的创建」详情页提供编辑/保存/取消按钮（`hermes_skillhub.js`）。

- **Knowledge base BFF proxy** — When `HERMES_INTEGRATION=1` and `KNOWLEDGE_BASE_URL` are set, `POST /api/integration/knowledge-base/*` proxies 13 downstream `POST /knowledge_base/*` endpoints. Caller passes `account` and `uuid` in the JSON body (or multipart form for `upload-docs`); WebUI does not call Zhiling identity lookup. Success returns upstream `data` only; upload requires `upload-docs` then `update-docs`. See `integration/knowledge_base/` and Swagger tag `IntegrationKnowledgeBase`.

- **Session manifest skill artifacts** — `GET /api/session/manifest` and SSE `manifest_delta` list conversation-created/updated skills in `artifacts[]` with `preview: "skill"`: from Hermes Agent `skill_manage` (`action`: `create`, `edit`, `patch`, `write_file`) and from generic write tools (`write_file`, `edit_file`, etc.) when the target path is `{HERMES_HOME}/skills/.../SKILL.md` under the session profile. Completed `skill_manage` results may supply nested `path` (e.g. `github/github-trending`). Requires integration enabled and `SKILL.md` on disk. Preview via `GET /api/skillhub/content`. `skill_view` remains in `references[]` only.

- **Session manifest MEDIA artifacts** — `GET /api/session/manifest` and SSE `manifest_delta` (`source.kind=turn_complete`) now include assistant-delivered local files from `MEDIA:<path>` tokens in `role=assistant` messages (`source_tool: media`). Workspace-relative paths preview via integration workspace file API; workspace-external absolute paths preview via `/api/media?path=&session_id=`. Per-turn chips still refresh from authoritative GET after turn `done`. Write-tool `source_tool` wins over `media` on the same path.

### Changed

- **Integration workspace files panel** — Opening the Workspace files rail no longer auto-previews the last selected file; the list may still restore selection highlight, and preview loads only after an explicit file click.

- **Integration workspace `ctime_ns` fallback** — `GET /api/integration/workspace/files` entries now populate `ctime_ns` from `st_ctime_ns` when `st_birthtime` is unavailable (e.g. Linux/Docker `/workspace`). No new response fields.

- **Integration workspace files search/filter/sort** — `GET /api/integration/workspace/files` supports `q` (basename contains), `type` (file extension filter, e.g. `.md`), `sort` (`path`/`size`/`mtime`/`ctime`), and `order` (default `desc`). Response entries include `ext`, `mime`, `mtime_ns`, `ctime_ns`; response adds `total`, `has_more`, and query echo. Left-rail UI uses server-side filtering/sorting (`hermes_integration_workspace.js`).

- **Knowledge base defaults in code** — `location`（create/edit 默认 `101`）与分页默认 `size`（`15`）改为 `integration/knowledge_base/constants.py` 常量，不再通过 `KNOWLEDGE_BASE_LOCATION` / `KNOWLEDGE_BASE_DEFAULT_PAGE_SIZE` 环境变量配置。调用方仍可在请求体中显式传入覆盖。

- **Integration workspace file API unified stream** — `GET /api/integration/workspace/file` now always returns raw file bytes with MIME by extension; no JSON text response, no `inline`/`download` query params, no `Content-Disposition`. Removed `GET /api/integration/workspace/file/raw`. Manifest and integration workspace rail preview use `fetch` + client-side rendering (HTML via sandboxed `srcdoc`).

- **SkillHub preview `scope=auto` (default)** — `GET /api/skillhub/content|structure|file` without `scope` (or `scope=auto`) resolves `{HERMES_HOME}/skills` first via `has_local_skill`, then falls back to SkillHub upstream. `scope=custom` / `scope=hub` remain local-only / hub-only. Manifest and workspace skill preview benefit without passing `scope=custom`.

- **Session manifest wire format** — `GET /api/session/manifest` and SSE `manifest_delta` rows now use slim `{path, preview, source_tool}` entries only (`preview`: `"file"` | `"skill"`). Non-previewable paths are omitted. File preview from manifest uses `/api/integration/workspace/file`; skills still use `/api/skillhub/content`. Removed `/api/file/allowlisted`.

### Fixed

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
