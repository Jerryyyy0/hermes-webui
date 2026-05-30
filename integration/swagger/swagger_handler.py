"""
Swagger UI 与 OpenAPI JSON 路由处理器。

提供两个端点：
  GET /docs            — Swagger UI 页面（静态资源在 integration/assets/swagger-ui/）
  GET /api/openapi.json — OpenAPI 3.0 规范 JSON（servers 按当前请求动态注入）
"""

from __future__ import annotations

import json
import os

from api.helpers import j, t

_SPEC_PATH = os.path.join(os.path.dirname(__file__), "openapi.json")

_SWAGGER_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Hermes WebUI — API Docs</title>
  <link rel="icon" type="image/svg+xml" href="static/integration/swagger-ui/favicon.svg" />
  <link rel="stylesheet" href="static/integration/swagger-ui/swagger-ui.css" />
  <style>
    body { margin: 0; }
    #swagger-ui .topbar { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="static/integration/swagger-ui/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "api/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
      layout: "BaseLayout",
      deepLinking: true,
      tryItOutEnabled: true,
      requestCredentials: "include"
    });
  </script>
</body>
</html>
"""


def _public_base_url(handler) -> str | None:
    """从当前 HTTP 请求推断对外 base URL（scheme + host[:port]）。"""
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    if proto not in ("http", "https"):
        try:
            from api.auth import _is_secure_context

            proto = "https" if _is_secure_context(handler) else "http"
        except Exception:
            proto = "http"

    host = (
        handler.headers.get("X-Forwarded-Host")
        or handler.headers.get("Host")
        or ""
    ).split(",", 1)[0].strip()
    if not host:
        return None
    return f"{proto}://{host}"


def _apply_dynamic_servers(spec: dict, handler) -> None:
    base = _public_base_url(handler)
    if base:
        spec["servers"] = [{"url": base, "description": "当前服务"}]
    else:
        spec["servers"] = [{"url": "/", "description": "当前站点"}]


def handle_swagger_ui(handler) -> bool:
    """返回 Swagger UI HTML 页面。"""
    t(handler, _SWAGGER_HTML, content_type="text/html; charset=utf-8")
    return True


def handle_openapi_json(handler) -> bool:
    """读取并返回 openapi.json 规范。"""
    try:
        with open(_SPEC_PATH, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except FileNotFoundError:
        j(handler, {"error": "openapi.json not found"}, status=404)
        return True
    except json.JSONDecodeError as e:
        j(handler, {"error": f"openapi.json parse error: {e}"}, status=500)
        return True
    _apply_dynamic_servers(spec, handler)
    j(handler, spec)
    return True
