"""Regression tests for Chinese apperror classification and payloads."""
from __future__ import annotations

from integration.chat_provider_errors import (
    append_persisted_provider_error_message,
    build_user_error_content,
    classify_connection_error_code,
    classify_provider_error,
    format_persisted_error_content,
    provider_error_payload,
    provider_error_payload_from_classification,
)


class TestConnectionErrorClassification:
    def test_connection_error_text_maps_to_unreachable(self):
        result = classify_provider_error('Connection error.', Exception('Connection error.'))

        assert result['type'] == 'connection_error'
        assert result['error_code'] == 'unreachable'
        assert result['label'] == '无法连接模型服务'
        assert '无法连接' in result['message']

    def test_connection_refused_subcode(self):
        assert classify_connection_error_code('connection refused at 127.0.0.1:8080') == 'connect_refused'
        result = classify_provider_error('connection refused at 127.0.0.1:8080')
        assert result['type'] == 'connection_error'
        assert result['error_code'] == 'connect_refused'
        assert result['label'] == '连接被拒绝'

    def test_timeout_subcode(self):
        result = classify_provider_error('request timed out after 30s')
        assert result['type'] == 'connection_error'
        assert result['error_code'] == 'timeout'
        assert result['label'] == '连接超时'


class TestChineseProviderErrorPayload:
    def test_payload_uses_chinese_message_and_preserves_raw_details(self, monkeypatch):
        secret = 'sk-proj-' + ('a' * 80)
        raw_error = 'Connection error. token=' + secret
        monkeypatch.setattr('integration.chat_provider_errors.payload._redact_text', lambda text: text.replace(secret, '[REDACTED]'))

        payload = provider_error_payload(
            raw_error,
            'connection_error',
            error_code='unreachable',
        )

        assert payload['label'] == '无法连接模型服务'
        assert '无法连接' in payload['message']
        assert payload['message'] != raw_error
        assert secret not in payload['message']
        assert payload['details'] == raw_error.replace(secret, '[REDACTED]')
        assert secret not in payload['details']
        assert payload['details_label'] == '技术详情'
        assert payload['type'] == 'connection_error'
        assert payload['error_code'] == 'unreachable'

    def test_payload_from_classification(self):
        classification = classify_provider_error('401 unauthorized')
        payload = provider_error_payload_from_classification('401 unauthorized', classification)

        assert payload['type'] == 'auth_mismatch'
        assert payload['label'] == '身份验证失败'
        assert 'API Key' in payload['message']
        assert '401 unauthorized' not in payload['message']
        assert payload['details'] == '401 unauthorized'

    def test_persisted_content_omits_empty_hint_markers(self):
        payload = provider_error_payload('', 'error')
        content = format_persisted_error_content(payload)

        assert content.startswith('**发生错误:**')
        assert not content.endswith('**')
        assert '\n\n**' not in content

    def test_generic_error_message_omits_raw_agent_text_in_message(self):
        raw = "No LLM provider configured. Run `hermes model` to select a provider."
        classification = classify_provider_error(raw)
        payload = provider_error_payload_from_classification(raw, classification)

        assert classification['type'] == 'error'
        assert raw not in payload['message']
        assert payload['details'] == raw


class _DummySession:
    def __init__(self):
        self.messages = []


class TestPersistedProviderErrorMessage:
    def test_append_persisted_provider_error_message(self):
        session = _DummySession()
        classification = classify_provider_error('Connection error.')
        payload = provider_error_payload_from_classification('Connection error.', classification)

        msg = append_persisted_provider_error_message(session, payload, err_type=classification['type'])

        assert session.messages[-1] is msg
        assert '**无法连接模型服务:**' in msg['content']
        assert msg['provider_details'] == 'Connection error.'
        assert msg['provider_details_label'] == '技术详情'
        assert msg['_error_type'] == 'connection_error'


class TestOtherErrorTypesRemainChinese:
    def test_quota_exhausted_is_chinese(self):
        result = classify_provider_error('Plan limit reached')
        assert result['type'] == 'quota_exhausted'
        assert result['label'] == '额度已用尽'

    def test_no_response_is_chinese(self):
        result = classify_provider_error('', None, silent_failure=True)
        assert result['type'] == 'no_response'
        assert result['label'] == '模型无响应'
        assert '模型服务' in build_user_error_content(err_type='no_response')['message']


class TestContentFilteredClassification:
    def test_data_inspection_failed_maps_to_content_filtered(self):
        raw = (
            'HTTP 400: data: {"error":{"code":"data_inspection_failed","param":null,'
            '"message":"Input text data may contain inappropriate content.",'
            '"type":"data_inspection_failed"},'
            # chatcmpl id contains "404" as a substring — must NOT trigger
            # model_not_found via naive '404' in err_str matching.
            '"id":"chatcmpl-95c64d9f-8364-4bd4-a89e-d06404ddf433"}'
        )
        result = classify_provider_error(raw)
        assert result['type'] == 'content_filtered'
        assert result['label'] == '内容被审核拦截'
        assert '审核策略' in result['message']
        assert raw not in result['message']

    def test_content_filter_keyword_maps_to_content_filtered(self):
        result = classify_provider_error('content_filter_triggered')
        assert result['type'] == 'content_filtered'

    def test_packy_content_exists_risk_maps_to_content_filtered(self):
        # PackyAPI / OpenRouter-compatible gateways return HTTP 400 with this
        # message instead of OpenAI-style content_filter / data_inspection_failed.
        raw = (
            'HTTP 400: Content Exists Risk '
            '(request id: 01KZD9Q6G33M5GQYHMRQES3R7Z)\n'
            "Details: {'message': 'Content Exists Risk "
            "(request id: 01KZD9Q6G33M5GQYHMRQES3R7Z)', "
            "'type': 'packy_invalid_request_error', 'param': '', "
            "'code': 'invalid_request_error'}"
        )
        result = classify_provider_error(raw)
        assert result['type'] == 'content_filtered'
        assert result['label'] == '内容被审核拦截'
        assert '审核策略' in result['message']
        assert 'Content Exists Risk' not in result['message']
        assert result['type'] != 'error'

    def test_content_exists_risk_snake_case_maps_to_content_filtered(self):
        result = classify_provider_error('content_exists_risk: blocked by gateway')
        assert result['type'] == 'content_filtered'

    def test_content_filtered_payload_preserves_raw_in_details(self):
        raw = 'data_inspection_failed: Input text data may contain inappropriate content.'
        classification = classify_provider_error(raw)
        payload = provider_error_payload_from_classification(raw, classification)
        assert payload['type'] == 'content_filtered'
        assert payload['label'] == '内容被审核拦截'
        assert raw not in payload['message']
        assert payload['details'] == raw
        assert payload['details_label'] == '审核详情'


class TestHttpStatusCodeMatching:
    """Naive 'NNN' in err_str substring matching misclassified chatcmpl/UUID
    substrings as HTTP status codes. These tests pin the strict matcher."""

    def test_404_inside_chatcmpl_id_does_not_trigger_model_not_found(self):
        raw = 'data_inspection_failed in chatcmpl-95c64d9f-8364-4bd4-a89e-d06404ddf433'
        result = classify_provider_error(raw)
        assert result['type'] == 'content_filtered'
        assert result['type'] != 'model_not_found'

    def test_401_inside_chatcmpl_id_does_not_trigger_auth_mismatch(self):
        raw = 'data_inspection_failed in chatcmpl-abc401def-1234-5678'
        result = classify_provider_error(raw)
        assert result['type'] == 'content_filtered'
        assert result['type'] != 'auth_mismatch'

    def test_429_inside_chatcmpl_id_does_not_trigger_rate_limit(self):
        raw = 'data_inspection_failed in chatcmpl-abc429def-1234-5678'
        result = classify_provider_error(raw)
        assert result['type'] == 'content_filtered'
        assert result['type'] != 'rate_limit'

    def test_real_http_404_still_triggers_model_not_found(self):
        result = classify_provider_error('HTTP 404: model not found')
        assert result['type'] == 'model_not_found'

    def test_real_http_401_still_triggers_auth_mismatch(self):
        result = classify_provider_error('HTTP 401: unauthorized')
        assert result['type'] == 'auth_mismatch'

    def test_real_http_429_still_triggers_rate_limit(self):
        result = classify_provider_error('HTTP 429: rate limit exceeded')
        assert result['type'] == 'rate_limit'
