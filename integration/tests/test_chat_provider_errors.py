"""Integration-layer tests for Chinese chat apperror helpers."""
from __future__ import annotations

from integration.chat_provider_errors import (
    append_persisted_provider_error_message,
    build_persisted_provider_error_message,
    classify_provider_error,
    provider_error_payload,
)


def test_connection_error_classification():
    result = classify_provider_error('Connection error.')
    assert result['type'] == 'connection_error'
    assert result['label'] == '无法连接模型服务'


def test_provider_error_payload_keeps_raw_details_in_details_only():
    payload = provider_error_payload('Connection error.', 'connection_error', error_code='unreachable')
    assert payload['message'] != 'Connection error.'
    assert 'Connection error.' not in payload['message']
    assert payload['details'] == 'Connection error.'


def test_persisted_error_builder_matches_append_shape():
    payload = provider_error_payload('Connection error.', 'connection_error', error_code='unreachable')
    built = build_persisted_provider_error_message(
        payload,
        err_type='connection_error',
        timestamp=123.0,
    )

    class Session:
        messages = []

    appended = append_persisted_provider_error_message(
        Session(),
        payload,
        err_type='connection_error',
    )

    assert built['timestamp'] == 123.0
    assert {key for key in built if key != 'timestamp'} == {key for key in appended if key != 'timestamp'}
    assert built['provider_details'] == 'Connection error.'
    assert built['provider_details_label'] == '技术详情'
