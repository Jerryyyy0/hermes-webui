"""Provider error classification for chat stream apperror events."""
from __future__ import annotations

import re

from integration.chat_provider_errors.messages import (
    build_user_error_content,
    cancelled_turn_hint,
)


# Matches a 3-digit HTTP status code only when it appears as a real status token
# (preceded by "HTTP ", a space, a colon, or start-of-string), not as a random
# substring inside a UUID/chatcmpl/JSON id. Examples that match:
#   "HTTP 404", " 404 ", "404 Not Found", "status: 404"
# Examples that do NOT match:
#   "chatcmpl-95c64d9f-8364-4bd4-a89e-d06404ddf433"  (404 inside UUID)
#   "id\":\"chatcmpl-...401...\""                     (401 inside chatcmpl id)
_HTTP_STATUS_RE = re.compile(r'(?:^|(?<=HTTP )|(?<=[ :]))\d{3}(?![0-9a-zA-Z])')


def _has_http_status(err_str: str, code: int) -> bool:
    """Return True only when ``code`` appears as a real HTTP status token.

    Naive ``str(code) in err_str`` substrings match inside UUIDs/chatcmpl IDs
    (e.g. ``d06404ddf433`` contains ``404``), producing false positives. This
    helper requires the digits to be delimited as a status token.
    """
    s = str(err_str or '')
    if not s:
        return False
    code_str = str(code)
    for m in _HTTP_STATUS_RE.finditer(s):
        if m.group(0) == code_str:
            return True
    return False


def classify_connection_error_code(err_str: str, exc=None) -> str | None:
    """Return a connection_error sub-code when the failure looks network-related."""
    err_str = str(err_str or '')
    _err_lower = err_str.lower()
    _exc_name = type(exc).__name__ if exc is not None else ''
    if exc is not None and _exc_name == 'ConnectionRefusedError':
        return 'connect_refused'
    if exc is not None and _exc_name in ('ConnectionError', 'ConnectError', 'TimeoutError'):
        if _exc_name == 'TimeoutError':
            return 'timeout'
        if 'refused' in _err_lower:
            return 'connect_refused'
        return 'unreachable'
    if 'connection refused' in _err_lower or 'errno 111' in _err_lower or 'econnrefused' in _err_lower:
        return 'connect_refused'
    if 'timed out' in _err_lower or 'timeout' in _err_lower or 'time out' in _err_lower:
        return 'timeout'
    if (
        'getaddrinfo' in _err_lower
        or 'name or service not known' in _err_lower
        or 'nodename nor servname' in _err_lower
        or 'temporary failure in name resolution' in _err_lower
    ):
        return 'dns'
    if 'ssl' in _err_lower or 'certificate' in _err_lower or 'cert_' in _err_lower:
        return 'ssl'
    if 'connection error' in _err_lower or 'api connection error' in _err_lower:
        return 'unreachable'
    if (
        'connection reset' in _err_lower
        or 'broken pipe' in _err_lower
        or 'network is unreachable' in _err_lower
        or 'failed to establish a new connection' in _err_lower
    ):
        return 'unreachable'
    return None


def is_quota_error_text(err_text: str) -> bool:
    """Return True when provider text looks like quota/usage exhaustion."""
    _err_lower = str(err_text or '').lower()
    return (
        'insufficient credit' in _err_lower
        or 'credit balance' in _err_lower
        or 'credits exhausted' in _err_lower
        or 'more credits' in _err_lower
        or 'can only afford' in _err_lower
        or 'fewer max_tokens' in _err_lower
        or 'quota_exceeded' in _err_lower
        or 'quota exceeded' in _err_lower
        or 'exceeded your current quota' in _err_lower
        or 'plan limit reached' in _err_lower
        or 'usage_limit_exceeded' in _err_lower
        or 'usage limit exceeded' in _err_lower
        or 'reached the limit of messages' in _err_lower
        or 'used up your usage' in _err_lower
        or ('plan' in _err_lower and 'limit' in _err_lower and 'reached' in _err_lower)
    )


def classify_provider_error(err_str: str, exc=None, *, silent_failure: bool = False) -> dict:
    """Classify provider/agent failure text for WebUI apperror UX."""
    err_str = str(err_str or '')
    _err_lower = err_str.lower()
    _exc_name = type(exc).__name__ if exc is not None else ''
    _is_cancelled = (
        'cancelled by user' in _err_lower
        or 'canceled by user' in _err_lower
        or 'user cancelled' in _err_lower
        or 'user canceled' in _err_lower
        or 'task cancelled' in _err_lower
        or 'task canceled' in _err_lower
        or 'cancellederror' in _err_lower
        or (exc is not None and _exc_name in ('CancelledError', 'CanceledError'))
    )
    _is_interrupted = (
        not _is_cancelled
        and (
            'interrupted by user' in _err_lower
            or 'response interrupted' in _err_lower
            or 'operation interrupted' in _err_lower
            or 'operation was interrupted' in _err_lower
            or 'operation aborted' in _err_lower
            or 'request was aborted' in _err_lower
            or 'aborterror' in _err_lower
            or (exc is not None and type(exc).__name__ in ('KeyboardInterrupt', 'AbortError'))
        )
    )

    def _result(err_type: str, *, error_code: str | None = None, hint_override: str | None = None) -> dict:
        user = build_user_error_content(
            err_type=err_type,
            error_code=error_code,
            raw_message=err_str,
        )
        hint = hint_override if hint_override is not None else user['hint']
        payload = {
            'label': user['label'],
            'message': user['message'],
            'type': err_type,
            'hint': hint,
        }
        if error_code:
            payload['error_code'] = error_code
        return payload

    if _is_cancelled:
        return _result('cancelled', hint_override=cancelled_turn_hint())
    if _is_interrupted:
        return _result('interrupted')
    _connection_code = classify_connection_error_code(err_str, exc)
    if _connection_code is not None:
        return _result('connection_error', error_code=_connection_code)
    _is_quota = is_quota_error_text(err_str)
    _is_auth = (
        _has_http_status(err_str, 401)
        or (exc is not None and 'AuthenticationError' in _exc_name)
        or 'authentication' in _err_lower
        or 'unauthorized' in _err_lower
        or 'invalid api key' in _err_lower
        or 'invalid_api_key' in _err_lower
        or 'no cookie auth credentials' in _err_lower
    )
    _is_not_found = (
        _has_http_status(err_str, 404)
        or 'not found' in _err_lower
        or 'does not exist' in _err_lower
        or 'model not found' in _err_lower
        or 'model_not_found' in _err_lower
        or 'invalid model' in _err_lower
        or 'does not match any known model' in _err_lower
        or 'unknown model' in _err_lower
    )
    _is_rate_limit = (
        'rate limit' in _err_lower
        or _has_http_status(err_str, 429)
        or (exc is not None and 'RateLimitError' in _exc_name)
    )
    _is_compression_exhausted = (
        'compression_exhausted' in _err_lower
        or 'compression exhausted' in _err_lower
        or ('context length exceeded' in _err_lower and 'cannot compress further' in _err_lower)
        or ('context compression' in _err_lower and 'max compression attempts' in _err_lower)
    )
    _is_content_filtered = (
        'data_inspection_failed' in _err_lower
        or 'content_filter' in _err_lower
        or 'content_policy_violation' in _err_lower
        or 'content policy violation' in _err_lower
        or 'content exists risk' in _err_lower
        or 'content_exists_risk' in _err_lower
        or 'moderation' in _err_lower
    )
    # Provider-specific codes (compression_exhausted, content_filtered) are
    # stronger signals than weak status-code/text heuristics (auth, not_found,
    # rate_limit). Check them first so a content-filtered 400 with a chatcmpl
    # id that happens to contain "404" is not misrouted to model_not_found.
    if _is_quota:
        return _result('quota_exhausted')
    if _is_compression_exhausted:
        return _result('compression_exhausted')
    if _is_content_filtered:
        return _result('content_filtered')
    if _is_rate_limit:
        return _result('rate_limit')
    if _is_auth:
        return _result('auth_mismatch')
    if _is_not_found:
        return _result('model_not_found')
    if silent_failure:
        return _result('no_response')
    return _result('error')
