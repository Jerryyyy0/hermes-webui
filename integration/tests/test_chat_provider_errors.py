"""Integration-layer tests for Chinese chat apperror helpers."""
from __future__ import annotations

from integration.chat_provider_errors import classify_provider_error, provider_error_payload


def test_connection_error_classification():
    result = classify_provider_error('Connection error.')
    assert result['type'] == 'connection_error'
    assert result['label'] == '无法连接模型服务'


def test_provider_error_payload_keeps_raw_details_in_details_only():
    payload = provider_error_payload('Connection error.', 'connection_error', error_code='unreachable')
    assert payload['message'] != 'Connection error.'
    assert 'Connection error.' not in payload['message']
    assert payload['details'] == 'Connection error.'
