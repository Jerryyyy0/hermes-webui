# Integration layer changelog

Fork 特有变更（SkillHub、profiles enrich、Swagger 等）记在此文件。**不要**为集成开发去改仓库根目录的 `CHANGELOG.md`，除非用户明确要求或该变更将合并进上游正式发布说明。

格式可参考根目录 `CHANGELOG.md` 的 `[Unreleased]` 小节。

## [Unreleased]

### Added

- **Cron Hub UI parity** — Cron Hub list/detail/forms reuse Tasks panel markup (`detail-card`, `detail-run-item`, `detail-form`) via `window.HermesCronShared` from `static/panels.js`.
- **Cron run session links** — `/api/crons/history` and `/api/crons/run` include `session_id` when a cron run can be materialized from `state.db`; Tasks and Cron Hub run history rows show an open-session action for full execution steps.
- **Cron fallback messages** — When a cron run has no `state.db` messages, materialize writes a two-message WebUI session: user = `job.prompt` only; assistant = run `.md` body (`source: cron_fallback`). Persists to sidecar JSON so follow-up chat keeps context. Does not overwrite sessions that already have messages.
- **Cross-profile cron (Cron Hub)** — `HERMES_INTEGRATION=1` enables `GET /api/crons?all_profiles=1` (grouped by owner profile), `GET /api/crons/recent?all_profiles=1` (completions with `owner_profile` + optional `session_id` backfill), `owner_profile` query on history/run/output, `POST /api/integration/crons/*` CRUD/run with explicit `owner_profile`, session materialize after runs (`integration/crons/session_bridge.py`), and UI panel `integrationCrons` (`hermes_integration_crons.js`).
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
