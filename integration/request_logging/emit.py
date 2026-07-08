"""Structured API error logging to the WebUI log stream."""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any

from api.helpers import _sanitize_error

from integration.request_logging.policy import (
    request_forwarded_for,
    request_method,
    request_path,
    request_remote,
    should_log_api_error,
)

_SUMMARY_MAX = 240
_MESSAGE_MAX = 500
_TRACEBACK_MAX = 8000


def _direct_write(text: str) -> None:
    try:
        stream = sys.stderr
        if stream is None:
            return
        stream.write(text if text.endswith("\n") else text + "\n")
        try:
            stream.flush()
        except Exception:
            pass
    except Exception:
        pass


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
    """Emit a structured ``api_error`` line. Never raises."""
    if not should_log_api_error(status):
        return

    try:
        safe_message = _bounded_text(_sanitize_error(message) if message else None, _MESSAGE_MAX)
        safe_error = _bounded_text(error_code, 200)
        record: dict[str, Any] = {
            "event": "api_error",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": request_method(handler),
            "path": request_path(handler),
            "status": status,
            "remote": request_remote(handler),
            "source": source,
        }
        forwarded_for = request_forwarded_for(handler)
        if forwarded_for:
            record["forwarded_for"] = forwarded_for
        if safe_error:
            record["error"] = safe_error
        if safe_message:
            record["message"] = safe_message

        tb_text = _format_traceback(exc_info) if status >= 500 else None
        if tb_text:
            record["traceback"] = tb_text

        line = f"[webui] {json.dumps(record, ensure_ascii=False)}"
        _direct_write(line)

        log_message = _build_summary(status=status, error_code=safe_error, message=safe_message)
        try:
            handler._api_error_summary = log_message
        except Exception:
            pass
    except Exception:
        try:
            _direct_write("[webui] {\"event\":\"api_error\",\"message\":\"emit_api_error failed\"}")
        except Exception:
            pass
