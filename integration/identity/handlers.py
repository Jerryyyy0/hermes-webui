"""HTTP handlers for Zhiling identity proxy (/api/integration/webui_login)."""

from __future__ import annotations

import time

from api.helpers import _sanitize_error, j

from integration.config import identity_lookup_enabled
from integration.request_logging.formatting import format_kv, one_line, with_timestamp
from integration.request_logging.logger import console_error, console_info, console_warning
from integration.identity.client import IdentityLookupError, lookup_current_identity
from integration.identity.session_store import (
    clear_session,
    get_cached_identity,
    save_session,
)

_NO_STORE = {"Cache-Control": "no-store"}


def _extract_bearer_token(handler) -> str:
    auth = str(handler.headers.get("Authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _request_remote(handler) -> str:
    try:
        if getattr(handler, "client_address", None):
            return str(handler.client_address[0])
    except Exception:
        pass
    return "-"


def _response_payload(payload: dict) -> dict:
    body = dict(payload)
    body["timestamp"] = int(time.time())
    return body


def _log_integration_login(handler, *, phase: str, **fields) -> None:
    """Human-readable trace for /api/integration/webui_login."""
    record = {
        "event": "integration_login",
        "phase": phase,
        "ts": time.time(),
        "remote": _request_remote(handler),
        **fields,
    }
    forwarded_for = str(handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded_for:
        record["forwarded_for"] = forwarded_for
    extras = format_kv({key: value for key, value in record.items() if key not in {"event", "phase", "ts"}})
    suffix = f" {extras}" if extras else ""
    line = with_timestamp(f"[webui][integration_login][{one_line(phase)}]{suffix}", record)
    status = record.get("status")
    if isinstance(status, int) and status >= 500:
        console_error(line)
    elif isinstance(status, int) and status >= 400:
        console_warning(line)
    else:
        console_info(line)


def try_handle_get(handler, parsed) -> bool:
    if not identity_lookup_enabled():
        return False
    if parsed.path != "/api/integration/webui_login":
        return False

    token = _extract_bearer_token(handler)
    if not token:
        status, payload = get_cached_identity()
        j(handler, _response_payload(payload), status=status, extra_headers=_NO_STORE)
        return True

    try:
        status, payload = lookup_current_identity(token)
    except IdentityLookupError as exc:
        _log_integration_login(
            handler,
            phase="exit",
            auth_mode="bearer",
            status=502,
            error="identity_lookup_failed",
        )
        j(
            handler,
            _response_payload(
                {
                    "error": "identity_lookup_failed",
                    "message": _sanitize_error(exc),
                }
            ),
            status=502,
            extra_headers=_NO_STORE,
        )
        return True

    if status == 200:
        save_session(token, payload)
    elif status == 401:
        clear_session()

    _log_integration_login(
        handler,
        phase="exit",
        auth_mode="bearer",
        status=status,
        cache_updated=status == 200,
        cache_cleared=status == 401,
        username=payload.get("username") if status == 200 and isinstance(payload, dict) else None,
        error=payload.get("error") if isinstance(payload, dict) else None,
    )
    j(handler, _response_payload(payload), status=status, extra_headers=_NO_STORE)
    return True
