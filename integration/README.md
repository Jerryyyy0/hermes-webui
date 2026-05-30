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

If you use a local HTTP proxy (`HTTP_PROXY`, e.g. Clash), add the SkillHub host to `NO_PROXY` (or rely on `ensure_skillhub_no_proxy()` at server startup, which appends the hostname from `SKILLHUB_URL`). Without this, `/api/skillhub/*` may return 502 while `curl` to the same upstream works.

### Profile enrich (`info.json`)

When integration is enabled, `GET /api/profiles` enriches each entry:

| Response field | Source |
|----------------|--------|
| `info` | `{profile.path}/info.json` (missing file → `{}`) |
| `info.logo` | Data URI base64 in info.json; invalid/over 100KB omitted from response |
| `skills` | Installed skills for that profile (`local_skills.list_installed`) |
| `memory_snapshot` | `{path}/memories/MEMORY.md`, `USER.md`, and `{path}/SOUL.md` (same fields as `GET /api/memory`, redacted) |

Write / update via UI or API:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/profile/logo-presets` | Built-in logo library (`?category=` optional) |
| POST | `/api/profile/info` | Update `info.json` (`display_name`, `description`, `logo_preset`, `logo_base64`, `remove_logo`) |

Example `info.json` — copy [`profiles/info.json.example`](profiles/info.json.example):

```json
{
  "display_name": "My Profile",
  "description": "Optional short description",
  "logo": "data:image/png;base64,..."
}
```

Regenerate built-in logo PNGs (network required): `python3 integration/scripts/fetch_profile_logos.py` (writes `assets/profile-logos/` from DiceBear + Noto Emoji; see `assets/profile-logos/LICENSES.md`).

Cron and Kanban profile pickers still show profile `name` only (by design).

### Cross-profile cron (`HERMES_INTEGRATION=1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/crons?all_profiles=1` | Grouped jobs: `{ profiles: [{ profile, jobs }] }` |
| GET | `/api/crons/recent?all_profiles=1&since=` | Cross-profile completions + session materialize |
| GET | `/api/crons/history`, `/run`, `/output` | Optional `?owner_profile=` (storage profile) |
| POST | `/api/integration/crons/create` | Create in `owner_profile` store (body: `owner_profile`, optional execution `profile`) |
| POST | `/api/integration/crons/update\|delete\|run\|pause\|resume` | Same; all require `owner_profile` |

UI: **Cron Hub** rail/sidebar (`integrationCrons`) via `hermes_integration_crons.js`. Upstream **Tasks** panel unchanged (single active profile). Global cron polling uses `all_profiles=1` when the flag is on.

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
| `GET /api/skillhub/content?name=` | `GET /api/skills/{name}/doc` |
| `GET /api/skillhub/structure?name=` | `GET /api/skills/{name}/structure` |
| `GET /api/skillhub/file?name=&path=` | `GET /api/skills/{name}/file?path=` |
| `POST /api/skillhub/install` | download/doc → `shared_skills_dir`；有 `category` 时 `skills/<category>/<name>/`，否则平铺 `skills/<name>/` |
| `POST /api/skillhub/delete` | remove local skill from `shared_skills_dir`（市场安装与 custom；仅需 `HERMES_INTEGRATION=1`） |
| `POST /api/skillhub/upload` | **仅本地** custom：`.md` / 多技能 `.zip` 或 JSON → `shared_skills_dir`；可选 `category` 分层；响应 `{ skill_count, file_count, skills[] }` |

`GET /api/skillhub/skills` annotates `installed` from `shared_skills_dir`. SkillHub routes do not use WebUI profile cookies or `profile` query/body parameters.

Query parameters:

| Param | Default | Meaning |
|-------|---------|---------|
| `scope` | `hub` | `hub` (market), `installed`, `not_installed` (`shared_skills_dir`), `custom` (`shared_skills_dir`) |
| `category` | `""` (all) | Hub category filter; empty/`all` = all categories |
| `q` | — | Search (list only; tab stats ignore `q`) |
| `page` / `page_size` | `1` / `20` | Pagination |

Response includes global `stats`: `{ hub, installed, not_installed, custom }` across **all** categories (unaffected by list `category` or `q`; only `skills`/`total` follow those filters).

## Layout

| Path | Role |
|------|------|
| `config.py` | `HERMES_INTEGRATION`, `SKILLHUB_URL`, `skillhub_enabled()` |
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

- `api/routes.py` — integration GET/POST dispatch, profiles enrich, static mapping, `__INTEGRATION_SKILLS__`, `__SKILLHUB_ENABLED__`
- `static/index.html` — integration scripts + SkillHub panel markup
- `static/panels.js` — `HermesProfiles` guard (`loadProfilesPanel`, `toggleProfileDropdown`, `renderProfileDetail`, `renderProfileForm`, `saveProfileForm`)
- `requirements.txt` — `httpx`
- `.env.example`

## Tests

```bash
pytest integration/tests/ tests/test_integration_seam.py -v
```
