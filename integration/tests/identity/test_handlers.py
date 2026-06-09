import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.identity.handlers import try_handle_get


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def test_handlers_noop_when_disabled():
    handler = MagicMock()
    parsed = urlparse("/api/integration/login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False


def test_missing_token_returns_400():
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    payload = _json_payload(handler)
    assert payload["error"] == "missing_token"


def test_success_passthrough_200():
    identity = {
        "username": "zhangsan",
        "ithinktank": {"account": "zhangsan", "userId": "abc"},
    }
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == identity


def test_upstream_401_passthrough():
    handler = MagicMock()
    handler.headers = {"Authorization": "bearer tok"}
    parsed = urlparse("/api/integration/login")
    detail = {"detail": "会话不存在或已超时，请重新登录。"}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(401, detail),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(401)
    assert _json_payload(handler) == detail


def test_upstream_403_passthrough():
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer tok"}
    parsed = urlparse("/api/integration/login")
    detail = {"detail": "无权查询其他用户身份。"}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(403, detail),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(403)
    assert _json_payload(handler) == detail


def test_lookup_unreachable_returns_502():
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer tok"}
    parsed = urlparse("/api/integration/login")
    from integration.identity.client import IdentityLookupError

    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            side_effect=IdentityLookupError("connection refused"),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(502)
    payload = _json_payload(handler)
    assert payload["error"] == "identity_lookup_failed"
    assert "connection refused" in payload["message"]
