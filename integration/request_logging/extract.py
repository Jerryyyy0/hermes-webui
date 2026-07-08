"""Extract error fields from API JSON payloads for structured logging."""

from __future__ import annotations

from typing import Any


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
    """Return (error_code, message) from a JSON response payload.

  Recognizes ``error``, ``message``, and ``msg`` only. Does not infer across fields.
    """
    if not isinstance(payload, dict):
        return None, None

    error_code = _bounded_string(payload.get("error"), 200)
    message = _bounded_string(payload.get("message"))
    if message is None:
        message = _bounded_string(payload.get("msg"))

    if message is None and error_code is not None:
        # ``bad()`` uses a single human-readable string in ``error``.
        if " " in error_code or any(ord(ch) > 127 for ch in error_code):
            message = error_code
            error_code = None

    return error_code, message
