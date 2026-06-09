"""HTTP client for Zhiling auth-proxy user-container logout."""

from __future__ import annotations

import httpx

from integration.config import zhiling_logout_base_url

_TIMEOUT = 10.0
_AUTH_PROXY_LOGOUT_PATH = "/api/logout"


class ZhilingLogoutError(Exception):
    """auth-proxy unreachable or misconfigured."""


def logout_current_user() -> tuple[int, dict]:
    """POST to auth-proxy /api/logout for the current user instance.

    Returns (status_code, json_body). Does not mutate upstream fields.
    """
    base = zhiling_logout_base_url()
    if not base:
        raise ZhilingLogoutError("ZHILING_LOGOUT_API_URL not configured")

    url = f"{base}{_AUTH_PROXY_LOGOUT_PATH}"

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={},
            )
    except httpx.HTTPError as exc:
        raise ZhilingLogoutError(str(exc)) from exc

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return resp.status_code, data
