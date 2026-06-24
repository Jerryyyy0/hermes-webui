# Hermes WebUI integration layer

Fork-specific features live here so upstream rebases stay predictable.

## Enable

```bash
export HERMES_INTEGRATION=1
export SKILLHUB_URL=http://127.0.0.1:8000   # optional; SkillHub market only (see docs/后端接口文档约束.md)
```

`HERMES_INTEGRATION=1` enables:

- **Profile enrich** — `GET /api/profiles` adds nested `info` (from `info.json`) and full `skills` array per profile. UI via `hermes_profiles.js` (logo picker, edit, create).
- **Cross-profile cron** — Cron Hub and grouped cron APIs across profiles.
- **SkillHub** — UI and `/api/skillhub/*` routes are active only when `SKILLHUB_URL` is also set.
- **Egress policy (iptables)** — gated API to apply iptables open/whitelist policies (see below). **Off by default**; requires `HERMES_EGRESS_POLICY_ENABLED=1`.
- **Knowledge base BFF** — `POST /api/integration/knowledge_base/*` routes are active only when `KNOWLEDGE_BASE_URL` is also set.

If you use a local HTTP proxy (`HTTP_PROXY`, e.g. Clash), add the SkillHub host to `NO_PROXY` (or rely on `ensure_skillhub_no_proxy()` at server startup, which appends the hostname from `SKILLHUB_URL`). Without this, `/api/skillhub/*` may return 502 while `curl` to the same upstream works.

### Profile enrich (`info.json`)

When integration is enabled, `GET /api/profiles` enriches each entry:

| Response field | Source |
|----------------|--------|
| `info` | `{profile.path}/info.json` (missing file → `{}`) |
| `info.logo` | Data URI base64 in info.json; invalid/over 4MB omitted from response |
| `skills` | Installed skills for that profile (`local_skills.list_installed`) |
| `memory_snapshot` | `{path}/memories/MEMORY.md`, `USER.md`, and `{path}/SOUL.md` (same fields as `GET /api/memory`, redacted) |
| `info.pinned` | `info.json` → `pinned: true`（仅置顶时返回） |
| `info.pin_order` | `info.json` → 置顶组内排序（越小越靠前；仅置顶时返回） |

`GET /api/profiles` 列表顺序：置顶 profiles（按 `pin_order`，含 `default`）→ 未置顶 `default` → 其余字母序。

Write / update via UI or API:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/profile/logo-presets` | Built-in logo library (`?category=` optional) |
| POST | `/api/profile/info` | Update `info.json` (`display_name`, `description`, `logo_preset`, `logo_base64`, `remove_logo`) |
| POST | `/api/profile/pin` | Pin/unpin profile (`name`, `pinned`); writes `pinned` + `pin_order` to `info.json` (max 5, includes `default`) |

Example `info.json` — copy [`profiles/info.json.example`](profiles/info.json.example):

```json
{
  "display_name": "My Profile",
  "description": "Optional short description",
  "logo": "data:image/png;base64,..."
}
```

Optional pin fields (usually set via `POST /api/profile/pin`, not hand-edited): `pinned` (`true`), `pin_order` (integer; `1` = topmost among pinned; each new pin becomes `1` and shifts older pins down).

Regenerate built-in logo PNGs (network required): `python3 integration/scripts/fetch_profile_logos.py` (writes `assets/profile-logos/` from DiceBear + Noto Emoji; see `assets/profile-logos/LICENSES.md`).

Cron and Kanban profile pickers still show profile `name` only (by design).

### Cross-profile cron (`HERMES_INTEGRATION=1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/crons?all_profiles=1` | Grouped jobs: `{ profiles: [{ profile, jobs }] }` |
| GET | `/api/crons/recent?all_profiles=1&since=` | Cross-profile completions + session materialize |
| GET | `/api/crons/history`, `/run`, `/output` | Optional `?profile=` (storage and execution profile) |
| GET | `/api/integration/crons/unread` | Cron Hub unread run counts across profiles |
| POST | `/api/integration/crons/create` | Create in the selected `profile` store and run under that same Profile (body: required `profile`, optional `skills`) |
| POST | `/api/integration/crons/update\|delete\|run\|pause\|resume` | Same; all require `profile` + `job_id`; delete also clears that job's materialized sessions, state rows, and `cron/output/<job_id>/` history |
| POST | `/api/integration/crons/unread/read` | Mark one Cron Hub job's current runs as read (`profile` + `job_id`) |

UI: **Cron Hub** rail/sidebar (`integrationCrons`) via `hermes_integration_crons.js`. Upstream **Tasks** panel unchanged (single active profile). Cron Hub creation requires one explicit Profile, stores the task there, runs it there, and can attach skills from that Profile. Global cron polling uses `all_profiles=1` when the flag is on.

When integration is enabled, one-shot schedules (`30m`, absolute datetimes, etc.) are kept in `jobs.json` after they finish (`enabled=false`, `state=completed`) instead of being auto-removed by Hermes Agent. Output history and Cron Hub listing remain available until you explicitly delete the job via `POST /api/integration/crons/delete` or upstream `POST /api/crons/delete`.

### Egress policy (iptables)

This is an **operator feature** that can modify the host firewall. It is **disabled by default**.

Enable explicitly:

```bash
export HERMES_INTEGRATION=1
export HERMES_EGRESS_POLICY_ENABLED=1
```

Optional overrides:

| Env | Default | Meaning |
|-----|---------|---------|
| `HERMES_EGRESS_POLICY_RULES_PATH` | `/etc/iptables/rules.v4` | Where to write the iptables-restore rules file before applying |

API:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/egress/policy` | Return `iptables -L -n` output |
| POST | `/api/integration/egress/policy` | Apply `open` or `whitelist` policy via `iptables-restore` |

POST body:

- `{"policy_type":"open"}`
- `{"policy_type":"whitelist","allowed_ips":["1.2.3.4","10.0.0.0/24"],"include_request_ip":true}`

Notes:

- `include_request_ip` is **opt-in**. The server will **not** silently add the request IP to the allowlist.
- `allowed_ips` accepts IP or CIDR; invalid entries are rejected.

### Zhiling 身份（Control Plane 代理）

在用户容器内，调用方从 zhiling 登录回调取得 `access_token`（`#/auth/callback#token=...`）后，可经 WebUI 转发查询当前用户平台身份与 i智库同步信息。首次带 Bearer 成功查询后，身份缓存在 WebUI 进程内存，后续可无 Bearer 读取；`access_token` 仅存服务端，读缓存响应不返回 token。完整 HTTP 契约见 [`docs/integration-login-api.md`](../docs/integration-login-api.md)。

启用：

```bash
export HERMES_INTEGRATION=1
export ZHILING_CONTROL_PLANE_URL=http://zhiling-control-plane:13001
# 可选：非 JWT 或 JWT 无 exp 时的缓存 TTL（秒，默认 1800）
# export ZHILING_IDENTITY_CACHE_TTL_SECONDS=1800
```

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/webui_login` | 代理 `GET {ZHILING_CONTROL_PLANE_URL}/api/identity/lookup`；带 Bearer 时刷新内存缓存，无 Bearer 时读缓存 |

请求头：

| Header | 必填 | 说明 |
|--------|------|------|
| `Authorization` | 条件必填 | 刷新缓存时：`Bearer <access_token>`；读缓存时可省略 |

若 WebUI 启用了密码/Passkey 鉴权，调用方还需携带有效的 `hermes_session` Cookie（`credentials: include`）；`Authorization` 仅用于 zhiling token，不与 WebUI Cookie 混用。

示例（经 WebUI 代理）：

```bash
TOKEN='前端拿到的 access_token'

# 首次 / 刷新
curl -sS \
  -H "Authorization: Bearer ${TOKEN}" \
  http://127.0.0.1:8787/api/integration/webui_login

# 后续读缓存
curl -sS http://127.0.0.1:8787/api/integration/webui_login
```

成功时响应体与 Control Plane 一致（原样透传，含 `username`、`organization`、`ithinktank` 等字段）。无缓存时为 HTTP `401` 且 `error` 为 `not_registered` 或 `session_expired`；无效 Bearer token 通常为 HTTP `401` 且 body 含 `detail`；Control Plane 不可达时 WebUI 返回 `502` 且 `error` 为 `identity_lookup_failed`。`POST /api/integration/webui_logout` 会清空身份缓存。

### Zhiling 用户容器登出（auth-proxy 代理）

在用户容器 compose 网络内，WebUI 后端通过 `ZHILING_LOGOUT_API_URL`（auth-proxy 根地址，不含路径）调用固定接口 `POST /api/logout`，识别当前用户实例（`EXPECTED_USERNAME`），不要求转发浏览器 Cookie。

启用：

```bash
export HERMES_INTEGRATION=1
export ZHILING_LOGOUT_API_URL=http://auth-proxy:8080
```

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/integration/webui_logout` | 清理 WebUI `hermes_session`，POST `{}` 至 auth-proxy，原样返回 JSON（含 `casdoor_logout_url`、`login_url` 等） |
| GET | `/api/integration/webui_logout` | 返回 `405` + `method_not_allowed` |

示例（容器内或经 WebUI 代理）：

```bash
curl -sS -X POST \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8787/api/integration/webui_logout
```

推荐流程：调用方收到 JSON 后由前端或门户跳转 `casdoor_logout_url`；WebUI 内置 Sign Out（`POST /api/auth/logout`）不修改，Zhiling 部署可单独调用本接口或浏览器同源 `POST /api/logout`（auth-proxy）。

auth-proxy 不可达时 WebUI 返回 `502` 且 `error` 为 `zhiling_logout_failed`。

### Workspace 无会话文件（`HERMES_INTEGRATION=1`）

根目录固定为 **`HERMES_WEBUI_DEFAULT_WORKSPACE`**（`api.config.DEFAULT_WORKSPACE`），**不需要 `session_id`**。相对路径均相对该根目录。

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/integration/workspace/files` | 平铺文件索引；`page`（默认 1）、`page_size`（默认 500，上限 5000）；可选子树 `path`（默认 `.`）；`q`（basename 包含搜索）、`type`（扩展名过滤，如 `.md`）、`sort`（`path`/`size`/`mtime`/`ctime`，默认 `path`）、`order`（`asc`/`desc`，默认 `desc`）；可选 `profile`（传入时仅返回该 profile 的 manifest 成果文件；不传则返回全部文件并对成果附加 `profile`）；可选 `refresh=1`（跳过服务端内存索引，强制重扫磁盘） |
| GET | `/api/integration/workspace/file` | 原始文件字节流（`path` 必填）；`Content-Type` 按扩展名；不设 `Content-Disposition` |
| POST | `/api/integration/workspace/file/delete` | 删除文件（`paths` 必填，字符串数组，至少 1 项）；仅删文件；响应 `deleted` / `failed`；全部失败 404 |

翻页：递增 `page` 直到响应 `has_more` 为 `false`。条目含 `ext`、`mime`、`mtime_ns`、`ctime_ns`（优先 birthtime，否则为 `st_ctime` 纳秒；`stat` 失败时为 `null`）。manifest 成果文件（会话 write 工具产出）附加可选 `profile`（`session.profile`）；非成果文件无该字段。

索引与读取默认排除系统/缓存垃圾文件（如 `.DS_Store`、`Thumbs.db`、`._*`），且不进入 `.git`、`node_modules`、`__pycache__` 等目录（与右侧 Workspace 文件树 #1793 规则一致）。

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?page=1&page_size=100'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?refresh=1'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?profile=ops'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/files?q=report&type=.md&sort=mtime&order=desc'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/file?path=README.md'
curl -sS 'http://127.0.0.1:8787/api/integration/workspace/file?path=assets/logo.png' -o logo.png
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/workspace/file/delete' \
  -H 'Content-Type: application/json' \
  -d '{"paths":["tmp/report.md","old.txt"]}'
```

UI（`HERMES_INTEGRATION=1`）：左侧 Rail / 移动顶栏 **Workspace 文件**（`integrationWorkspace`），`hermes_integration_workspace.js` + `hermes_integration_workspace.css`。左栏为平铺列表（服务端搜索/类型过滤/排序、分页「加载更多」、单行删除与多选批量删除、刷新），中间主区只读预览（文本 / Markdown / 图片 / PDF / HTML / 媒体）。删除后会刷新 session manifest，成果 chip 可标为已过期。与会话绑定的右侧 Workspace 面板（`/api/list` + `session_id`）并存。

### 知识库 BFF 代理（`KNOWLEDGE_BASE_URL`）

在用户容器或门户内，前端将 `account` / `uuid` 放入请求体，经 WebUI 转发至下游知识库服务（`POST {KNOWLEDGE_BASE_URL}/knowledge_base/*`）。后端不调用 Zhiling identity lookup。

启用：

```bash
export HERMES_INTEGRATION=1
export KNOWLEDGE_BASE_URL=http://192.168.1.132:17861
```

| Method | Path | 下游 | 调用方必填 |
|--------|------|------|-----------|
| POST | `/api/integration/knowledge_base/list` | `list_ps_knowledge_bases` | `account`, `uuid`, `isPersonal` |
| POST | `/api/integration/knowledge_base/joined` | `user_joined_shkbs` | `account`, `uuid` |
| POST | `/api/integration/knowledge_base/create` | `create_ps_kb` | `account`, `uuid`, `showName`, `isPersonal` |
| POST | `/api/integration/knowledge_base/info` | `show_ps_kb_info` | `kbName` |
| POST | `/api/integration/knowledge_base/edit` | `edit_kb_information` | `kbName`, `showName` |
| POST | `/api/integration/knowledge_base/delete` | `delete_ps_kb` | `account`, `kbName` |
| POST | `/api/integration/knowledge_base/available` | `available_shkbs` | `account`, `uuid`, `page`, `size` |
| POST | `/api/integration/knowledge_base/apply_join` | `apply_join_shkb` | `account`, `uuid`, `kbName` |
| POST | `/api/integration/knowledge_base/members` | `get_user_inshkb` | `uuid`, `kbName`, `page`, `size` |
| POST | `/api/integration/knowledge_base/documents` | `list_knowledge_bases_details` | `kbName`, `page`, `size` |
| POST | `/api/integration/knowledge_base/upload_docs` | `upload_docs` | multipart：`uuid`, `kbName`, `files`, `fileProperties` |
| POST | `/api/integration/knowledge_base/update_docs` | `update_docs` | `kbName`, `fileNames`, `fileProperties` |
| POST | `/api/integration/knowledge_base/delete_docs` | `delete_docs` | `kbName`, `fileNames` |
| POST | `/api/integration/knowledge_base/show_pdf` | `show_pdf` | `kbName`, `fileName`（可选 `flag`） |
| POST | `/api/integration/knowledge_base/search_docs` | `search_docs` | `query`, `kbName`（可选 `topK`, `scoreThreshold`） |
| POST | `/api/integration/knowledge_base/search_docs_xcore` | `search_docs_xcore` | `query`, `kbNames`（非空数组；可选 `topK`, `scoreThreshold`） |

成功时 HTTP 状态码与 JSON body **原样透传**下游响应（含 `code` / `msg` / `data` 包装）。`show_pdf` 在下游返回 PDF 时透传二进制。BFF 自身错误：请求校验失败 HTTP 400；下游不可达 HTTP 502。

文档上传须两步串联：`upload_docs` 成功后再 `update_docs`。

```bash
curl -sS -X POST http://127.0.0.1:8787/api/integration/knowledge_base/list \
  -H "Content-Type: application/json" \
  -d '{"account":"admin","uuid":"aaaaaaaa0000aaaa0000aaaaaaaaaaaa","isPersonal":1}'
```

## 维护约束

1. **不要改根目录 `CHANGELOG.md`** — 集成外部接口、接缝文件或本目录代码时，发布说明写在 [`CHANGELOG.md`](CHANGELOG.md)（本文件）。根目录 `CHANGELOG.md` 留给上游同步，除非 Maintainer 明确要求。
2. **改接口必更 Swagger** — 变更 `/api/skillhub/*` 或其它 integration 路由时，同步更新 [`swagger/openapi.json`](swagger/openapi.json)（`GET /docs`、`GET /api/openapi.json`）。参数、响应 envelope、错误码须与 `skills/handlers.py` 等实现一致。

## API routes

### Local skills (unchanged — `api/routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/skills` | Installed skills for active profile |
| GET | `/api/skills/content` | Local SKILL.md / linked files |
| POST | `/api/skills/save`, `/delete`, `/toggle` | Local CRUD |

### SkillHub proxy (`integration/skills/handlers.py` → `SKILLHUB_URL`)

| WebUI | Upstream |
|-------|----------|
| `GET /api/skillhub/skills` | `GET /api/skills` (`scope`, `q`, `category`, `page`, `page_size` — no `profile` upstream) |
| `GET /api/skillhub/categories` | `GET /api/skills/categories` |
| `GET /api/skillhub/detail?name=` | `GET /api/skills/{name}` |
| `GET /api/skillhub/content?name=` | 默认 `scope=auto`：本地 `{HERMES_HOME}/skills` 优先，否则 `GET /api/skills/{name}/doc` |
| `GET /api/skillhub/structure?name=` | 同上（`structure`） |
| `GET /api/skillhub/file?name=&path=` | 同上（`file`） |
| `POST /api/skillhub/install` | download/doc → `shared_skills_dir`；有 `category` 时 `skills/<category>/<name>/`，否则平铺 `skills/<name>/` |
| `POST /api/skillhub/delete` | remove local skill from `shared_skills_dir`（市场安装与 custom；仅需 `HERMES_INTEGRATION=1`） |
| `GET /api/skillhub/download` | **仅本地**：zip 内 `{leaf}/` 目录 + `.skill-origin.json` sidecar；排除 `.hub_installed` 等元数据；`name` + 可选 `dir_name`；仅需 `HERMES_INTEGRATION=1` |
| `POST /api/skillhub/edit` | **仅本地** custom：更新已有技能的 `SKILL.md`（`name` + `content`，可选 `dir_name`）；市场安装不可编辑；仅需 `HERMES_INTEGRATION=1` |
| `POST /api/skillhub/upload` | **仅本地** custom：`.md` / 多技能 `.zip` 或 JSON → `shared_skills_dir`；可选 `category`、`dir_name`、`overwrite`（按 frontmatter `name` 覆盖全部 custom 副本）；响应 `{ skill_count, file_count, skills[] }` |

`GET /api/skillhub/skills` annotates `installed` from `shared_skills_dir`. SkillHub routes do not use WebUI profile cookies or `profile` query/body parameters.

Query parameters:

| Param | Default | Meaning |
|-------|---------|---------|
| `scope` | `hub` | `hub` (market), `installed`, `not_installed` (`shared_skills_dir`), `custom` (`shared_skills_dir`) |
| `category` | `""` (all) | Hub category filter; empty/`all` = all categories |
| `q` | — | Search (list only; tab stats ignore `q`) |
| `page` / `page_size` | `1` / `20` | Pagination |
| `sort` | `name` | `name` or `mtime` |
| `order` | `asc` | `asc` or `desc` |

List items always include aligned string fields (empty string when unset) and `mtime` (`null` when unset). Hub upstream `updated_at` is normalized into `mtime`. `hub` / `installed` / `not_installed` fetch the full catalog locally, apply `q` as substring match, sort, then paginate.

Response includes global `stats`: `{ hub, installed, not_installed, custom }` across **all** categories (unaffected by list `category` or `q`; only `skills`/`total` follow those filters).

## Layout

| Path | Role |
|------|------|
| `config.py` | `HERMES_INTEGRATION`, `SKILLHUB_URL`, `KNOWLEDGE_BASE_URL`, `ZHILING_CONTROL_PLANE_URL`, `ZHILING_LOGOUT_API_URL`, `ZHILING_IDENTITY_CACHE_TTL_SECONDS`, `skillhub_enabled()`, `knowledge_base_enabled()`, `identity_lookup_enabled()`, `zhiling_identity_cache_ttl_seconds()`, `zhiling_logout_enabled()` |
| `knowledge_base/` | `/api/integration/knowledge_base/*` → `{KNOWLEDGE_BASE_URL}/knowledge_base/*` |
| `identity/` | `GET /api/integration/webui_login` → Control Plane `/api/identity/lookup`；进程内身份缓存（`session_store.py`） |
| `logout/` | `POST /api/integration/webui_logout` → `{ZHILING_LOGOUT_API_URL}/api/logout` |
| `skills/skillhub.py` | Upstream httpx client |
| `skills/handlers.py` | `/api/skillhub/*` HTTP handlers |
| `profiles/` | `GET /api/profiles` enrich; `POST /api/profile/info`; `GET /api/profile/logo-presets` |
| `scripts/fetch_profile_logos.py` | Generate built-in logo library |
| `assets/profile-logos/` | Logo preset PNGs + manifest |
| `assets/hermes_skillhub.js` | SkillHub sidebar panel |
| `assets/hermes_profiles.js` | Profiles panel enrich |
| `swagger/openapi.json` | Integration API 规范（`GET /api/openapi.json` 动态 `servers`） |
| `swagger/swagger_handler.py` | `GET /docs`、`GET /api/openapi.json` |
| `assets/swagger-ui/` | 离线 Swagger UI（`swagger-ui-dist@5.18.2`，经 `/static/integration/swagger-ui/*` 提供） |
| `CHANGELOG.md` | Fork 集成层发布说明（勿写入根目录 CHANGELOG） |

## Upstream seam files (only these should conflict on rebase)

- `api/routes.py` — integration GET/POST dispatch, profiles enrich, static mapping, `__INTEGRATION_SKILLS__`, `__SKILLHUB_ENABLED__`; `GET /api/sessions` calls `_apply_integration_sidebar_session_filters` to drop cron execution rows when `HERMES_INTEGRATION=1`
- `static/index.html` — integration scripts + SkillHub panel markup
- `static/panels.js` — `HermesProfiles` guard (`loadProfilesPanel`, `toggleProfileDropdown`, `renderProfileDetail`, `renderProfileForm`, `saveProfileForm`)
- `requirements.txt` — `httpx`
- `.env.example`

## Tests

```bash
pytest integration/tests/ tests/test_integration_seam.py -v
```
