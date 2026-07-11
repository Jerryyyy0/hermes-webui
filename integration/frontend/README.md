# x_frontend via Nginx

Serve `x_frontend/dist` from Nginx and proxy API/SSE to Hermes WebUI. **No WebUI backend code changes.**

## Prerequisites

- WebUI listening on `127.0.0.1:8787` (`curl -s http://127.0.0.1:8787/health`)
- Dist at `/Users/wzq/Downloads/NLP-PyProject/x_frontend/dist` (edit `root` in the conf if different)
- Homebrew nginx (`/opt/homebrew/bin/nginx`)

## Enable

```bash
# From hermes-webui repo root
ln -sf "$(pwd)/integration/frontend/nginx-x-frontend.conf" \
  /opt/homebrew/etc/nginx/leadnews.conf/hermes-x-frontend.conf

# Homebrew nginx.conf uses relative error_log under the Cellar prefix
mkdir -p "$(nginx -V 2>&1 | sed -n 's/.*--prefix=\([^ ]*\).*/\1/p')/logs"

nginx -t
# If nginx is not running (or pid file empty):
nginx
# If already running:
nginx -s reload
```

Open **http://127.0.0.1:8080** (SPA). Direct **http://127.0.0.1:8787** still shows the upstream `static/` UI.

## Skip login (local, nginx-only)

`x_frontend` hardcodes Casdoor at `http://192.168.1.139:23008/`. This conf handles that **only in nginx**:

1. Rewrite SPA JS — no `location.assign` to Casdoor; always mount the app shell
2. Inject `app_auth_session` into `index.html`
3. Mock `GET /api/integration/webui_login` from [`mock-webui-login.json`](mock-webui-login.json) and `POST /api/integration/webui_logout` (not proxied to WebUI)

Hard-refresh the browser (or clear site data for `:8080`) after reload so cached JS is not reused.

To restore real SSO later, remove the mock `location = /api/integration/webui_login` / `webui_logout` blocks and the JS `sub_filter` rules, then `nginx -s reload`.

## Layout

| Path | Source |
|------|--------|
| `/api/integration/webui_login`, `/api/integration/webui_logout` | nginx mock (local skip-login) |
| other `/api/*`, `/health`, `/docs`, `/extensions/`, `/plugins/` | proxy → `127.0.0.1:8787` |
| `/assets/*`, `/pdfjs/*`, root files | `x_frontend/dist` |
| `/static/*` | dist first, else backend (e.g. swagger-ui) |
| other routes | SPA `index.html` |

SSE uses `proxy_buffering off` and a 3600s read timeout. `Host` is forwarded as `$http_host` so same-origin CSRF matches the browser origin on port 8080.

## Change port / paths

Edit `listen`, `root`, or `upstream hermes_webui_backend` in [`nginx-x-frontend.conf`](nginx-x-frontend.conf), then `nginx -t && nginx -s reload`.
