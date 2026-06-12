# Hermes external integration (in-repo)

Fork-specific WebUI extensions live under [`integration/`](../integration/README.md).

## Seam files (upstream merge touch points)

| File | Change |
|------|--------|
| [`api/routes.py`](../api/routes.py) | `try_handle_get` / `try_handle_post_early` / `try_handle_post`; profiles enrich; `/static/integration/*`; `__INTEGRATION_SKILLS__`, `__SKILLHUB_ENABLED__`; integration egress policy route dispatch |
| [`static/index.html`](../static/index.html) | SkillHub panel + `hermes_skillhub.js` |
| [`static/panels.js`](../static/panels.js) | `switchPanel('skillhub')`; `HermesProfiles` guard (profiles panel, detail, create form) |
| [`.env.example`](../.env.example) | `HERMES_INTEGRATION`, `SKILLHUB_URL`, `KNOWLEDGE_BASE_URL` |

## API summary

- **`GET /api/skills`** — local installed skills (original WebUI; not intercepted by integration).
- **`GET /api/skillhub/*`** — SkillHub upstream proxy when `SKILLHUB_URL` is set. Upstream contract: [`docs/后端接口文档约束.md`](后端接口文档约束.md).
- **`GET /api/profiles`** — enrich adds nested `info`, `skills`, and `memory_snapshot` (MEMORY.md / USER.md / SOUL.md per profile) when integration enabled; `POST /api/profile/info`, `GET /api/profile/logo-presets`.
- **`GET /api/integration/crons/unread`, `POST /api/integration/crons/unread/read`** — Cron Hub unread run counts and per-job read cursors.
- **`GET/POST /api/integration/egress/policy`** — operator-only iptables policy API, gated by `HERMES_EGRESS_POLICY_ENABLED=1`.
- **`GET /api/integration/login`** — Zhiling Control Plane identity lookup proxy when `HERMES_INTEGRATION=1` and `ZHILING_CONTROL_PLANE_URL` are set; requires `Authorization: Bearer <access_token>`. See [`docs/integration-login-api.md`](integration-login-api.md).
- **`POST /api/integration/logout`** — Zhiling auth-proxy user-container logout proxy when `HERMES_INTEGRATION=1` and `ZHILING_LOGOUT_API_URL` (auth-proxy origin) are set; POSTs to `{base}/api/logout`; clears WebUI session cookie and returns auth-proxy JSON (e.g. `casdoor_logout_url`). Does not change built-in Sign Out UI.
- **`GET /api/integration/workspace/files`**, **`/file`** — Session-less workspace file list (paginated flat index) and raw file stream under `HERMES_WEBUI_DEFAULT_WORKSPACE` when `HERMES_INTEGRATION=1`.
- **`POST /api/integration/knowledge-base/*`** — Knowledge base downstream BFF proxy when `HERMES_INTEGRATION=1` and `KNOWLEDGE_BASE_URL` are set. Caller supplies `account` / `uuid` in the request body (no identity lookup). Upload is two-step: `upload-docs` then `update-docs`.

See [`integration/README.md`](../integration/README.md) for the full route table.

## Maintainer constraints

| Topic | Rule |
|-------|------|
| Release notes | Fork/integration work → [`integration/CHANGELOG.md`](../integration/CHANGELOG.md). Avoid editing root [`CHANGELOG.md`](../CHANGELOG.md) during integration unless explicitly requested. |
| API docs | Any change to integration HTTP routes → update [`integration/swagger/openapi.json`](../integration/swagger/openapi.json) in the same change; verify at `/docs`. |
