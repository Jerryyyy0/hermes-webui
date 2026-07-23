"""HTTP client for Zhiling Control Plane identity lookup."""

from __future__ import annotations

import logging

import httpx

from integration.config import zhiling_control_plane_url
from integration.project_logging import get_logger

logger = get_logger(__name__)
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
    except httpx.HTTPError as exc:
        logger.warning("identity lookup request failed: %s", type(exc).__name__)
        raise IdentityLookupError(str(exc)) from exc

    logger.info("identity lookup completed status=%s", resp.status_code)

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return resp.status_code, data
