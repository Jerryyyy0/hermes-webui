import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.identity.handlers import try_handle_post
from integration.identity.session_store import clear_session, get_cached_identity, save_session


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def _set_cookie_header(handler: MagicMock) -> str:
    for call in handler.send_header.call_args_list:
        if call.args[0] == "Set-Cookie":
            return call.args[1]
    return ""


def setup_function():
    clear_session()


def test_password_change_noop_when_disabled():
    handler = MagicMock()
    parsed = urlparse("/api/integration/auth/password/change")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=False):
        assert try_handle_post(handler, parsed, {}) is False


def test_password_change_noop_for_other_paths():
    handler = MagicMock()
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        assert try_handle_post(handler, urlparse("/api/integration/webui_login"), {}) is False


def test_password_change_success_passthrough_and_clears_sessions():
    upstream = {
        "success": True,
        "code": 200,
        "message": "密码修改成功，请重新登录",
        "reauth_required": True,
        "request_id": "req-1",
    }
    body = {
        "username": "test721",
        "old_password": "Sgitg@2026",
        "new_password": "NewPassword2027",
    }
    handler = MagicMock()
    handler.headers = {"Cookie": "hermes_session=abc.def"}
    parsed = urlparse("/api/integration/auth/password/change")
    save_session("tok", {"username": "cached"})

    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.change_password",
            return_value=(200, upstream),
        ):
            with patch("api.auth.parse_cookie", return_value="abc.def"):
                with patch("api.auth.invalidate_session") as invalidate:
                    assert try_handle_post(handler, parsed, body) is True
    invalidate.assert_called_once_with("abc.def")
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream
    set_cookie = _set_cookie_header(handler)
    assert "hermes_session=" in set_cookie
    assert "max-age=0" in set_cookie.lower()
    status, payload = get_cached_identity()
    assert status == 401
    assert payload == {"error": "not_registered"}


def test_password_change_401_passthrough_without_clearing_cookie():
    upstream = {
        "success": False,
        "code": 401,
        "message": "用户名或旧密码错误",
        "request_id": "req-2",
    }
    handler = MagicMock()
    handler.headers = {"Cookie": "hermes_session=abc.def"}
    parsed = urlparse("/api/integration/auth/password/change")
    save_session("tok", {"username": "cached"})

    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.change_password",
            return_value=(401, upstream),
        ):
            with patch("api.auth.invalidate_session") as invalidate:
                assert try_handle_post(handler, parsed, {"username": "x"}) is True
    invalidate.assert_not_called()
    handler.send_response.assert_called_with(401)
    assert _json_payload(handler) == upstream
    status, payload = get_cached_identity()
    assert status == 200
    assert payload == {"username": "cached"}


def test_password_change_400_passthrough():
    upstream = {
        "success": False,
        "code": 402,
        "message": "新密码不能与旧密码相同",
        "request_id": "req-3",
    }
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/auth/password/change")

    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.change_password",
            return_value=(400, upstream),
        ):
            assert try_handle_post(handler, parsed, {}) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler) == upstream


def test_password_change_unreachable_returns_502():
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/auth/password/change")
    from integration.identity.client import PasswordChangeError

    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.change_password",
            side_effect=PasswordChangeError("connection refused"),
        ):
            assert try_handle_post(handler, parsed, {}) is True
    handler.send_response.assert_called_with(502)
    payload = _json_payload(handler)
    assert payload["error"] == "password_change_failed"
    assert "connection refused" in payload["message"]
