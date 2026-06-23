"""Chinese apperror copy, classification, and persistence helpers for chat streams.

Fork-specific user-facing error UX for SSE ``apperror`` events. ``api/streaming.py``
imports this package so upstream streaming code stays thin.

Submodules:
- ``messages`` — ``CHAT_ERROR_ZH`` copy table and label builders
- ``classify`` — provider error type detection
- ``payload`` — SSE payload shaping and session persistence
"""
from __future__ import annotations

from integration.chat_provider_errors.classify import (
    classify_connection_error_code,
    classify_provider_error,
    is_quota_error_text,
)
from integration.chat_provider_errors.messages import (
    CANCELLED_TURN_HINT,
    CHAT_ERROR_ZH,
    build_user_error_content,
    cancelled_turn_hint,
    provider_details_label_for_type,
)
from integration.chat_provider_errors.payload import (
    _redact_text,
    append_persisted_provider_error_message,
    format_persisted_error_content,
    provider_error_payload,
    provider_error_payload_from_classification,
)

__all__ = [
    'CANCELLED_TURN_HINT',
    'CHAT_ERROR_ZH',
    '_redact_text',
    'append_persisted_provider_error_message',
    'build_user_error_content',
    'cancelled_turn_hint',
    'classify_connection_error_code',
    'classify_provider_error',
    'format_persisted_error_content',
    'is_quota_error_text',
    'provider_details_label_for_type',
    'provider_error_payload',
    'provider_error_payload_from_classification',
]
