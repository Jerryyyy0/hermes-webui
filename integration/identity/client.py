"""HTTP client for Zhiling Control Plane identity lookup."""

from __future__ import annotations

import json
from typing import Any

import httpx

from integration.config import zhiling_control_plane_url
from integration.project_logging import (
    console_info,
    console_warning,
    format_kv,
    get_logger,
    one_line,
    with_timestamp,
)
from integration.project_logging.formatting import is_sensitive_key

logger = get_logger(__name__)
_TIMEOUT = 10.0


class IdentityLookupError(Exception):
    """Control Plane unreachable or misconfigured."""


class PasswordChangeError(Exception):
    """Control Plane password change unreachable or misconfigured."""


def _redact_for_log(value: Any) -> Any:
    """Recursively redact sensitive keys before logging response bodies."""
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


def _log_downstream_response(
    *,
    event: str,
    log_prefix: str,
    status: int,
    data: Any,
    raw_text: str = "",
) -> None:
    """Log full Control Plane response body (no truncation)."""
    body_type = type(data).__name__
    keys = "-"
    if isinstance(data, dict):
        keys = ",".join(sorted(str(k) for k in data.keys())) or "-"
    try:
        redacted = _redact_for_log(data) if isinstance(data, (dict, list)) else data
        body_text = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        body_text = str(raw_text or data)
    # Keep body as a single physical line, but never truncate content.
    body_text = one_line(body_text, max_len=0)
    meta = format_kv(
        {
            "status": status,
            "body_type": body_type,
            "keys": keys,
        }
    )
    # Append body outside format_kv — format_kv applies a 240-char one_line cap per value.
    line = with_timestamp(
        f"[webui][{log_prefix}] downstream response {meta} body={body_text}",
        {"event": event, "phase": "response", "status": status},
    )
    if status >= 400:
        console_warning(line)
    else:
        console_info(line)


def _log_lookup_response(*, status: int, data: Any, raw_text: str = "") -> None:
    _log_downstream_response(
        event="identity_lookup",
        log_prefix="integration_login][identity_lookup",
        status=status,
        data=data,
        raw_text=raw_text,
    )


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

    raw_text = ""
    try:
        raw_text = resp.text or ""
    except Exception:
        raw_text = ""

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        _log_lookup_response(status=resp.status_code, data=data, raw_text=raw_text)
        data = {}
    else:
        _log_lookup_response(status=resp.status_code, data=data, raw_text=raw_text)
    return resp.status_code, data


def change_password(body: dict) -> tuple[int, dict]:
    """Forward password change to Control Plane /api/auth/password/change.

    Returns (status_code, json_body). Does not mutate upstream fields.
    """
    base = zhiling_control_plane_url()
    if not base:
        raise PasswordChangeError("ZHILING_CONTROL_PLANE_URL not configured")

    url = f"{base}/api/auth/password/change"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as exc:
        logger.warning("password change request failed: %s", type(exc).__name__)
        raise PasswordChangeError(str(exc)) from exc

    logger.info("password change completed status=%s", resp.status_code)

    raw_text = ""
    try:
        raw_text = resp.text or ""
    except Exception:
        raw_text = ""

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        _log_downstream_response(
            event="password_change",
            log_prefix="integration_auth][password_change",
            status=resp.status_code,
            data=data,
            raw_text=raw_text,
        )
        data = {}
    else:
        _log_downstream_response(
            event="password_change",
            log_prefix="integration_auth][password_change",
            status=resp.status_code,
            data=data,
            raw_text=raw_text,
        )
    return resp.status_code, data
