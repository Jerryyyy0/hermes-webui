import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.identity.session_store import clear_session, get_cached_identity, save_session
from integration.logout.handlers import try_handle_get, try_handle_post


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def _set_cookie_header(handler: MagicMock) -> str:
    for call in handler.send_header.call_args_list:
        if call.args[0] == "Set-Cookie":
            return call.args[1]
    return ""


def test_handlers_noop_when_disabled():
    handler = MagicMock()
    parsed = urlparse("/api/integration/webui_logout")
    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False
        assert try_handle_post(handler, parsed, {}) is False


def test_get_returns_405_method_not_allowed():
    handler = MagicMock()
    parsed = urlparse("/api/integration/webui_logout")
    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(405)
    assert _json_payload(handler) == {"status": "error", "error": "method_not_allowed"}


def test_success_passthrough_200_and_clears_webui_cookie():
    upstream = {
        "status": "ok",
        "username": "user-cuihao",
        "login_url": "http://192.168.1.139:23002/login",
        "control_plane_logout_url": "http://192.168.1.139:23001/api/auth/logout",
        "casdoor_logout_url": "http://192.168.1.139:23008/api/sso-logout",
        "cleared_cookie_names": ["zhiling_agent_session", "zhiling_agent_session_state"],
    }
    handler = MagicMock()
    handler.headers = {"Cookie": "hermes_session=abc.def"}
    parsed = urlparse("/api/integration/webui_logout")
    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=True):
        with patch(
            "integration.logout.handlers.logout_current_user",
            return_value=(200, upstream),
        ):
            with patch("api.auth.parse_cookie", return_value="abc.def"):
                with patch("api.auth.invalidate_session") as invalidate:
                    assert try_handle_post(handler, parsed, {}) is True
    invalidate.assert_called_once_with("abc.def")
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream
    set_cookie = _set_cookie_header(handler)
    assert "hermes_session=" in set_cookie
    assert "max-age=0" in set_cookie.lower()


def test_upstream_non_2xx_passthrough():
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_logout")
    detail = {"status": "error", "error": "method_not_allowed"}
    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=True):
        with patch(
            "integration.logout.handlers.logout_current_user",
            return_value=(405, detail),
        ):
            assert try_handle_post(handler, parsed, {}) is True
    handler.send_response.assert_called_with(405)
    assert _json_payload(handler) == detail


def test_logout_unreachable_returns_502():
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_logout")
    from integration.logout.client import ZhilingLogoutError

    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=True):
        with patch(
            "integration.logout.handlers.logout_current_user",
            side_effect=ZhilingLogoutError("connection refused"),
        ):
            assert try_handle_post(handler, parsed, {}) is True
    handler.send_response.assert_called_with(502)
    payload = _json_payload(handler)
    assert payload["error"] == "zhiling_logout_failed"
    assert "connection refused" in payload["message"]


def test_logout_clears_zhiling_identity_cache():
    clear_session()
    save_session("tok", {"username": "cached"})
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_logout")
    with patch("integration.logout.handlers.zhiling_logout_enabled", return_value=True):
        with patch(
            "integration.logout.handlers.logout_current_user",
            return_value=(200, {"status": "ok"}),
        ):
            assert try_handle_post(handler, parsed, {}) is True
    status, payload = get_cached_identity()
    assert status == 401
    assert payload == {"error": "not_registered"}
