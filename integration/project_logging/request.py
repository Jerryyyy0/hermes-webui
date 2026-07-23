"""HTTP request access, API error, and slow-request logging."""

from __future__ import annotations

import logging
import sys
import time
import traceback
from typing import Any
from urllib.parse import urlsplit

from integration.project_logging.config import log_error, log_warning, resolve_log_level
from integration.project_logging.formatting import format_api_error_line

_SUMMARY_MAX = 240
_MESSAGE_MAX = 500
_TRACEBACK_MAX = 8000


def api_error_logging_enabled() -> bool:
    return True


def api_error_min_status() -> int:
    return 400


def should_log_api_error(status: int) -> bool:
    if not api_error_logging_enabled():
        return False
    if status < api_error_min_status():
        return False
    level = resolve_log_level()
    if status >= 500:
        return level <= logging.ERROR
    return level <= logging.WARNING


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


def request_id(handler) -> str | None:
    try:
        value = getattr(handler, "_request_id", None)
        if value:
            return str(value)
    except Exception:
        pass
    return None


def _bounded_string(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def error_fields_from_payload(payload: Any) -> tuple[str | None, str | None]:
    """Return (error_code, message) from a JSON response payload."""
    if not isinstance(payload, dict):
        return None, None

    error_code = _bounded_string(payload.get("error"), 200)
    message = _bounded_string(payload.get("message"))
    if message is None:
        message = _bounded_string(payload.get("msg"))

    if message is None and error_code is not None:
        if " " in error_code or any(ord(ch) > 127 for ch in error_code):
            message = error_code
            error_code = None

    return error_code, message


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _format_traceback(exc_info) -> str | None:
    if exc_info is None:
        return None
    try:
        if exc_info is True:
            exc_info = sys.exc_info()
        etype, evalue, etb = exc_info
        if etype is None:
            return None
        text = "".join(traceback.format_exception(etype, evalue, etb)).rstrip()
        return _bounded_text(text, _TRACEBACK_MAX)
    except Exception:
        return None


def _build_summary(*, status: int, error_code: str | None, message: str | None) -> str:
    parts: list[str] = [str(status)]
    if error_code:
        parts.append(error_code)
    if message and message != error_code:
        parts.append(message)
    summary = " | ".join(parts)
    return summary[:_SUMMARY_MAX]


def emit_api_error(
    handler,
    *,
    status: int,
    message: str | None = None,
    error_code: str | None = None,
    exc_info=None,
    source: str = "j",
) -> None:
    """Emit a human-readable ``api_error`` line. Never raises."""
    if not should_log_api_error(status):
        return

    try:
        from api.helpers import _sanitize_error

        safe_message = _bounded_text(_sanitize_error(message) if message else None, _MESSAGE_MAX)
        safe_error = _bounded_text(error_code, 200)
        record: dict[str, Any] = {
            "event": "api_error",
            "ts": time.time(),
            "method": request_method(handler),
            "path": request_path(handler),
            "status": status,
            "remote": request_remote(handler),
            "source": source,
        }
        forwarded_for = request_forwarded_for(handler)
        if forwarded_for:
            record["forwarded_for"] = forwarded_for
        rid = request_id(handler)
        if rid:
            record["request_id"] = rid
        if safe_error:
            record["error"] = safe_error
        if safe_message:
            record["message"] = safe_message

        tb_text = _format_traceback(exc_info) if status >= 500 else None
        if tb_text:
            record["traceback"] = tb_text

        log_line = format_api_error_line(record)
        if status >= 500:
            log_error(log_line)
            if tb_text:
                log_error(tb_text)
        else:
            log_warning(log_line)

        log_message = _build_summary(status=status, error_code=safe_error, message=safe_message)
        try:
            handler._api_error_summary = log_message
        except Exception:
            pass
    except Exception:
        try:
            log_error("[webui][api_error] emit failed")
        except Exception:
            pass


def maybe_log_api_response(handler, status: int, payload, *, exc_info=None, source: str = "j") -> None:
    """Log an API error response derived from ``j()`` / ``bad()``."""
    error_code, message = error_fields_from_payload(payload)
    emit_api_error(
        handler,
        status=status,
        message=message,
        error_code=error_code,
        exc_info=exc_info,
        source=source,
    )
