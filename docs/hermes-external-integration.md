# Hermes external integration (in-repo)

Fork-specific WebUI extensions live under [`integration/`](../integration/README.md).

## Seam files (upstream merge touch points)

| File | Change |
|------|--------|
| [`api/routes.py`](../api/routes.py) | `try_handle_get` / `try_handle_post_early` / `try_handle_post`; profiles enrich; `/static/integration/*`; `__INTEGRATION_SKILLS__`, `__SKILLHUB_ENABLED__` |
| [`static/index.html`](../static/index.html) | SkillHub panel + `hermes_skillhub.js` |
| [`static/panels.js`](../static/panels.js) | `switchPanel('skillhub')`; `HermesProfiles` guard (profiles panel, detail, create form) |
| [`.env.example`](../.env.example) | `HERMES_INTEGRATION`, `SKILLHUB_URL` |

## API summary

- **`GET /api/skills`** — local installed skills (original WebUI; not intercepted by integration).
- **`GET /api/skillhub/*`** — SkillHub upstream proxy when `SKILLHUB_URL` is set. Upstream contract: [`docs/后端接口文档约束.md`](后端接口文档约束.md).
- **`GET /api/profiles`** — enrich adds nested `info`, `skills`, and `memory_snapshot` (MEMORY.md / USER.md / SOUL.md per profile) when integration enabled; `POST /api/profile/info`, `GET /api/profile/logo-presets`.

See [`integration/README.md`](../integration/README.md) for the full route table.

## Maintainer constraints

| Topic | Rule |
|-------|------|
| Release notes | Fork/integration work → [`integration/CHANGELOG.md`](../integration/CHANGELOG.md). Avoid editing root [`CHANGELOG.md`](../CHANGELOG.md) during integration unless explicitly requested. |
| API docs | Any change to integration HTTP routes → update [`integration/swagger/openapi.json`](../integration/swagger/openapi.json) in the same change; verify at `/docs`. |
