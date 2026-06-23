"""Apperror payload shaping and session persistence for chat streams."""
from __future__ import annotations

import time
from typing import Callable

from api.helpers import _redact_text as _default_redact_text

from integration.chat_provider_errors.messages import (
    build_user_error_content,
    provider_details_label_for_type,
)

# Tests may monkeypatch this module attribute.
_redact_text = _default_redact_text


def format_persisted_error_content(error_payload: dict) -> str:
    label = str(error_payload.get('label') or '发生错误')
    message = str(error_payload.get('message') or '')
    hint = str(error_payload.get('hint') or '').strip()
    content = f'**{label}:** {message}'
    if hint:
        content += f'\n\n*{hint}*'
    return content


def provider_error_payload(
    raw_message: str,
    err_type: str,
    hint: str = '',
    *,
    label: str = '',
    error_code: str | None = None,
    redact_text: Callable[[str], str] | None = None,
) -> dict:
    """Build apperror payload: Chinese copy in message; raw error in details only."""
    _redact = redact_text or _redact_text
    user = build_user_error_content(
        err_type=err_type,
        error_code=error_code,
        raw_message=raw_message,
    )
    _safe_raw = _redact(str(raw_message or '')).strip() if raw_message else ''
    payload: dict = {
        'label': label or user['label'],
        'message': user['message'],
        'type': err_type,
        'hint': hint or user['hint'],
        'details_label': provider_details_label_for_type(err_type),
    }
    if error_code:
        payload['error_code'] = error_code
    if _safe_raw:
        _details = _safe_raw
        if len(_details) > 1200:
            _details = _details[:1197].rstrip() + '…'
        payload['details'] = _details
    return payload


def provider_error_payload_from_classification(raw_message: str, classification: dict) -> dict:
    return provider_error_payload(
        raw_message,
        classification['type'],
        classification.get('hint', ''),
        label=classification.get('label', ''),
        error_code=classification.get('error_code'),
    )


def append_persisted_provider_error_message(session, error_payload: dict, *, err_type: str) -> dict:
    """Persist an assistant error turn and return the stored message dict."""
    _error_message = {
        'role': 'assistant',
        'content': format_persisted_error_content(error_payload),
        'timestamp': int(time.time()),
        '_error': True,
        '_error_type': err_type,
    }
    if error_payload.get('details'):
        _error_message['provider_details'] = error_payload['details']
    _details_label = error_payload.get('details_label') or provider_details_label_for_type(err_type)
    if _details_label and error_payload.get('details'):
        _error_message['provider_details_label'] = _details_label
    session.messages.append(_error_message)
    return _error_message
