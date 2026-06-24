"""HTTP client for Zhiling Control Plane identity lookup."""

from __future__ import annotations

import httpx

from integration.config import zhiling_control_plane_url

_TIMEOUT = 10.0


class IdentityLookupError(Exception):
    """Control Plane unreachable or misconfigured."""


def lookup_current_identity(access_token: str) -> tuple[int, dict]:
    """Forward Bearer token to Control Plane /api/identity/lookup.

    Returns (status_code, json_body). Does not mutate upstream fields.
    """
    base = zhiling_control_plane_url()
    if not base:
        raise IdentityLookupError("ZHILING_CONTROL_PLANE_URL not configured")

    url = f"{base}/api/identity/lookup"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            print(f"==============/api/identity/lookup响应：{resp.json()}==============")
    except httpx.HTTPError as exc:
        raise IdentityLookupError(str(exc)) from exc

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return resp.status_code, data
