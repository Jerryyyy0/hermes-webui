"""Chinese apperror copy, classification, and persistence helpers for chat streams.

Fork-specific user-facing error UX for SSE ``apperror`` events. ``api/streaming.py``
and ``api/models.py`` import this package so upstream code stays thin.

Submodules:
- ``messages`` — ``CHAT_ERROR_ZH`` copy table and label builders
- ``classify`` — provider error type detection
- ``payload`` — SSE payload shaping and session persistence
- ``interruption_copy`` — interrupted-turn recovery marker wording (zh)
"""
from __future__ import annotations

from integration.chat_provider_errors.classify import (
    classify_connection_error_code,
    classify_provider_error,
    is_quota_error_text,
)
from integration.chat_provider_errors.interruption_copy import (
    INTERRUPTED_NEUTRAL_ZH,
    INTERRUPTED_NO_OUTPUT_ZH,
    INTERRUPTED_PENDING_RETRY_ZH,
    INTERRUPTED_RECOVERED_ZH,
    INTERRUPTION_CAUSE_DETAILS_ZH,
    RECOVERY_CONTROL_HINT_ZH,
    RECOVERY_CONTROL_MESSAGE_ZH,
    build_interrupted_content_zh,
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
    'INTERRUPTED_NEUTRAL_ZH',
    'INTERRUPTED_NO_OUTPUT_ZH',
    'INTERRUPTED_PENDING_RETRY_ZH',
    'INTERRUPTED_RECOVERED_ZH',
    'INTERRUPTION_CAUSE_DETAILS_ZH',
    'RECOVERY_CONTROL_HINT_ZH',
    'RECOVERY_CONTROL_MESSAGE_ZH',
    '_redact_text',
    'append_persisted_provider_error_message',
    'build_interrupted_content_zh',
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
