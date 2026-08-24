"""HTTP handler for the read-only integration configuration endpoint."""

from __future__ import annotations

import os

from api.helpers import bad, j
from integration.config import integration_enabled

_CONFIG_PATH = "/api/integration/config"
_BROWSER_PREVIEW_URL = "BROWSER_PREVIEW_URL"


def try_handle_get(handler, parsed) -> bool:
    """Return the effective browser preview URL."""
    if not integration_enabled():
        return False
    if parsed.path != _CONFIG_PATH:
        return False

    value = os.environ.get(_BROWSER_PREVIEW_URL)
    if value is None:
        bad(handler, f"{_BROWSER_PREVIEW_URL} 未配置", status=500)
        return True

    j(handler, {"browser_preview_url": value})
    return True
