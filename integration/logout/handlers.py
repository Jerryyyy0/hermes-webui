"""HTTP handlers for Zhiling auth-proxy logout proxy (/api/integration/webui_logout)."""

from __future__ import annotations

import http.cookies

from api.helpers import _sanitize_error, j

from integration.config import zhiling_logout_enabled
from integration.logout.client import ZhilingLogoutError, logout_current_user


def _clear_webui_session_cookie_header() -> str:
    from api.auth import COOKIE_NAME

    cookie = http.cookies.SimpleCookie()
    cookie[COOKIE_NAME] = ""
    cookie[COOKIE_NAME]["httponly"] = True
    cookie[COOKIE_NAME]["path"] = "/"
    cookie[COOKIE_NAME]["max-age"] = "0"
    return cookie[COOKIE_NAME].OutputString()


def _invalidate_webui_session(handler) -> None:
    from api.auth import invalidate_session, parse_cookie

    cookie_val = parse_cookie(handler)
    if cookie_val:
        invalidate_session(cookie_val)


def _logout_extra_headers(handler) -> dict[str, str]:
    _invalidate_webui_session(handler)
    return {
        "Cache-Control": "no-store",
        "Set-Cookie": _clear_webui_session_cookie_header(),
    }


def try_handle_get(handler, parsed) -> bool:
    if not zhiling_logout_enabled():
        return False
    if parsed.path != "/api/integration/webui_logout":
        return False

    j(
        handler,
        {"status": "error", "error": "method_not_allowed"},
        status=405,
        extra_headers={"Cache-Control": "no-store"},
    )
    return True


def try_handle_post(handler, parsed, body) -> bool:
    if not zhiling_logout_enabled():
        return False
    if parsed.path != "/api/integration/webui_logout":
        return False

    extra_headers = _logout_extra_headers(handler)

    from integration.identity.session_store import clear_session

    clear_session()

    try:
        status, payload = logout_current_user()
    except ZhilingLogoutError as exc:
        j(
            handler,
            {
                "error": "zhiling_logout_failed",
                "message": _sanitize_error(exc),
            },
            status=502,
            extra_headers=extra_headers,
        )
        return True

    j(handler, payload, status=status, extra_headers=extra_headers)
    return True
