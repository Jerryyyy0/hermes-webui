"""Zhiling split WebUI CSRF bypass when auth-proxy is the trust boundary.

The browser talks to the shared frontend; requests are proxied through
control-plane/auth-proxy to this backend. Hermes built-in CSRF check assumes
browser and backend share the same origin, so it rejects legitimate POSTs as
cross-origin mismatch. In integration mode, auth-proxy is the trust boundary
and injects Authorization / X-Forwarded-User after validating the user.
"""

from __future__ import annotations

import logging
import traceback
from types import SimpleNamespace

logger = logging.getLogger(__name__)

_installed = False

# Shared frontend origins that proxy to this WebUI backend.
_SPLIT_WEBUI_ORIGIN_PREFIXES = (
    "http://192.168.1.139:23003",
    "http://47.93.211.132:23003",
)


def _origin_allowed_for_split_webui(origin: str) -> bool:
    lowered = (origin or "").lower()
    return any(lowered.startswith(prefix.lower()) for prefix in _SPLIT_WEBUI_ORIGIN_PREFIXES)


def split_webui_csrf_bypass(handler) -> bool:
    """Return True when auth-proxy-trusted split-WebUI request may skip CSRF."""
    from integration.config import integration_enabled

    if not integration_enabled():
        return False
    origin = (handler.headers.get("Origin", "") or handler.headers.get("Referer", "")).lower()
    if not _origin_allowed_for_split_webui(origin):
        return False
    return bool(handler.headers.get("Authorization") or handler.headers.get("X-Forwarded-User"))


def install_zhiling_split_webui_csrf_hook() -> None:
    """Monkey-patch api.routes._check_csrf for Zhiling split WebUI deployments."""
    global _installed
    if _installed:
        return
    _installed = True

    try:
        import api.routes as routes_module

        original = routes_module._check_csrf

        def _zhiling_split_webui_check_csrf(handler):
            if split_webui_csrf_bypass(handler):
                return True
            return original(handler)

        routes_module._check_csrf = _zhiling_split_webui_check_csrf
    except Exception as exc:
        traceback.print_exc()
        logger.exception("Failed to install Zhiling split WebUI CSRF hook error=%s", exc)


def _fake_handler(**headers):
    return SimpleNamespace(headers=headers)
