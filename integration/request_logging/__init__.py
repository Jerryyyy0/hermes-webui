"""Structured API error logging for Hermes WebUI integration layer."""

from integration.request_logging.emit import emit_api_error
from integration.request_logging.extract import error_fields_from_payload

__all__ = [
    "emit_api_error",
    "error_fields_from_payload",
    "maybe_log_api_response",
]


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
