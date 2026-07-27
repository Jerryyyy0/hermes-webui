"""HTTP handlers for Zhiling identity proxy (/api/integration/webui_login)."""

from __future__ import annotations

import json
import time
from typing import Any

from api.helpers import _sanitize_error, j

from integration.config import identity_lookup_enabled
from integration.identity.client import (
    IdentityLookupError,
    PasswordChangeError,
    change_password,
    lookup_current_identity,
)

from integration.project_logging import (
    console_error,
    console_info,
    console_warning,
    format_kv,
    one_line,
    with_timestamp,
)
from integration.project_logging.formatting import is_sensitive_key
from integration.identity.session_store import (
    clear_session,
    get_cached_identity,
    save_session,
)

_NO_STORE = {"Cache-Control": "no-store"}
_PASSWORD_CHANGE_PATH = "/api/integration/auth/password/change"


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


def _redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = _redact_for_log(item)
        return out
    if isinstance(value, list):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _log_outgoing_response(*, status: int, body: dict, auth_mode: str) -> None:
    """Print the exact webui_login response body before sending to the client."""
    try:
        body_text = json.dumps(_redact_for_log(body), ensure_ascii=False, separators=(",", ":"))
    except Exception:
        body_text = one_line(body, max_len=0)
    body_text = one_line(body_text, max_len=0)
    keys = ",".join(sorted(str(k) for k in body.keys())) if isinstance(body, dict) else "-"
    meta = format_kv({"status": status, "auth_mode": auth_mode, "keys": keys})
    line = with_timestamp(
        f"[webui][integration_login][response] outgoing {meta} body={body_text}",
        {
            "event": "integration_login",
            "phase": "response",
            "status": status,
            "auth_mode": auth_mode,
        },
    )
    if status >= 400:
        console_warning(line)
    else:
        console_info(line)


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
        response_body = _response_payload(payload)
        _log_outgoing_response(status=status, body=response_body, auth_mode="cache")
        j(handler, response_body, status=status, extra_headers=_NO_STORE)
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
        response_body = _response_payload(
            {
                "error": "identity_lookup_failed",
                "message": _sanitize_error(exc),
            }
        )
        _log_outgoing_response(status=502, body=response_body, auth_mode="bearer")
        j(handler, response_body, status=502, extra_headers=_NO_STORE)
        return True

    # sync_stats = None
    if status == 200:
        save_session(token, payload)
        # Temporarily disabled: sync ithink_kb_mcp headers on login.
        # if isinstance(payload, dict):
        #     try:
        #         from integration.identity.mcp_headers_sync import sync_ithink_kb_mcp_headers
        #
        #         sync_stats = sync_ithink_kb_mcp_headers(payload)
        #     except Exception as exc:
        #         console_warning(
        #             with_timestamp(
        #                 f"[webui][integration_login][mcp_headers] {one_line(f'sync failed: {exc}')}",
        #                 {
        #                     "event": "integration_login",
        #                     "phase": "mcp_headers_failed",
        #                     "error": str(exc),
        #                 },
        #             )
        #         )
        #     else:
        #         if isinstance(sync_stats, dict):
        #             extras = format_kv(
        #                 {
        #                     "updated": sync_stats.get("updated"),
        #                     "skipped": sync_stats.get("skipped"),
        #                     "errors": sync_stats.get("errors"),
        #                 }
        #             )
        #             summary = with_timestamp(
        #                 f"[webui][integration_login][mcp_headers] sync finished {extras}".rstrip(),
        #                 {
        #                     "event": "integration_login",
        #                     "phase": "mcp_headers_done",
        #                     **{k: sync_stats.get(k) for k in ("updated", "skipped", "errors")},
        #                 },
        #             )
        #             if sync_stats.get("errors"):
        #                 console_warning(summary)
        #             else:
        #                 console_info(summary)
    elif status == 401:
        clear_session()

    log_fields = {
        "auth_mode": "bearer",
        "status": status,
        "cache_updated": status == 200,
        "cache_cleared": status == 401,
        "username": payload.get("username") if status == 200 and isinstance(payload, dict) else None,
        "error": payload.get("error") if isinstance(payload, dict) else None,
    }
    # if isinstance(sync_stats, dict):
    #     log_fields["mcp_headers_updated"] = sync_stats.get("updated")
    #     log_fields["mcp_headers_skipped"] = sync_stats.get("skipped")
    #     log_fields["mcp_headers_errors"] = sync_stats.get("errors")
    _log_integration_login(handler, phase="exit", **log_fields)
    response_body = _response_payload(payload if isinstance(payload, dict) else {})
    _log_outgoing_response(status=status, body=response_body, auth_mode="bearer")
    j(handler, response_body, status=status, extra_headers=_NO_STORE)
    return True


def try_handle_post(handler, parsed, body) -> bool:
    if not identity_lookup_enabled():
        return False
    if parsed.path != _PASSWORD_CHANGE_PATH:
        return False

    request_body = body if isinstance(body, dict) else {}
    try:
        status, payload = change_password(request_body)
    except PasswordChangeError as exc:
        j(
            handler,
            {
                "success": False,
                "error": "password_change_failed",
                "message": f"修改密码服务不可用：{_sanitize_error(exc)}",
            },
            status=502,
            extra_headers=_NO_STORE,
        )
        return True

    extra_headers = dict(_NO_STORE)
    if status == 200 and isinstance(payload, dict) and payload.get("reauth_required") is True:
        from integration.logout.handlers import _logout_extra_headers

        extra_headers = _logout_extra_headers(handler)
        clear_session()

    j(
        handler,
        payload if isinstance(payload, dict) else {},
        status=status,
        extra_headers=extra_headers,
    )
    return True
