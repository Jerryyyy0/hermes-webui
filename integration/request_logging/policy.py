"""Policy helpers for API error logging."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


def api_error_logging_enabled() -> bool:
    raw = os.getenv("HERMES_WEBUI_API_ERROR_LOG", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def api_error_min_status() -> int:
    raw = os.getenv("HERMES_WEBUI_API_ERROR_LOG_MIN_STATUS", "400").strip()
    try:
        return int(raw)
    except ValueError:
        return 400


def should_log_api_error(status: int) -> bool:
    if not api_error_logging_enabled():
        return False
    return status >= api_error_min_status()


def request_method(handler) -> str:
    return str(getattr(handler, "command", None) or "-")


def request_path(handler) -> str:
    raw = str(getattr(handler, "path", None) or "-")
    if raw == "-":
        return raw
    try:
        return urlsplit(raw).path or "/"
    except Exception:
        return raw.split("?", 1)[0] or "/"


def request_remote(handler) -> str:
    try:
        if getattr(handler, "client_address", None):
            return str(handler.client_address[0])
    except Exception:
        pass
    return "-"


def request_forwarded_for(handler) -> str | None:
    try:
        headers = getattr(handler, "headers", None)
        if headers is None:
            return None
        value = str(headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return value or None
    except Exception:
        return None
