"""HTTP handlers for Zhiling identity proxy (/api/integration/login)."""

from __future__ import annotations

from api.helpers import _sanitize_error, bad, j

from integration.config import identity_lookup_enabled
from integration.identity.client import IdentityLookupError, lookup_current_identity


def _extract_bearer_token(handler) -> str:
    auth = str(handler.headers.get("Authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def try_handle_get(handler, parsed) -> bool:
    if not identity_lookup_enabled():
        return False
    if parsed.path != "/api/integration/login":
        return False

    token = _extract_bearer_token(handler)
    if not token:
        bad(handler, "missing_token", status=400)
        return True

    try:
        status, payload = lookup_current_identity(token)
    except IdentityLookupError as exc:
        j(
            handler,
            {
                "error": "identity_lookup_failed",
                "message": _sanitize_error(exc),
            },
            status=502,
        )
        return True

    j(handler, payload, status=status)
    return True
