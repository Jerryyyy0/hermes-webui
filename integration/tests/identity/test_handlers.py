import json
import re
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.identity.handlers import try_handle_get
from integration.identity.session_store import clear_session, get_cached_identity

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def _without_timestamp(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "timestamp"}


def setup_function():
    clear_session()


def test_handlers_noop_when_disabled():
    handler = MagicMock()
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False


def test_missing_token_returns_not_registered():
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(401)
    payload = _json_payload(handler)
    assert payload["error"] == "not_registered"
    assert isinstance(payload.get("timestamp"), int)


def _patch_mcp_sync():
    return patch(
        "integration.identity.mcp_headers_sync.sync_ithink_kb_mcp_headers",
        return_value={"updated": 0, "skipped": 0, "errors": 0},
    )


def test_success_passthrough_200_and_caches_identity():
    identity = {
        "username": "zhangsan",
        "ithinktank": {"account": "zhangsan", "userId": "abc"},
    }
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with _patch_mcp_sync():
                assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    payload = _json_payload(handler)
    assert _without_timestamp(payload) == identity
    assert isinstance(payload.get("timestamp"), int)
    status, cached = get_cached_identity()
    assert status == 200
    assert cached == identity


def test_cached_read_without_bearer():
    identity = {"username": "cached-user"}
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with _patch_mcp_sync():
                try_handle_get(handler, parsed)

    handler.headers = {}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    payload = _json_payload(handler)
    assert _without_timestamp(payload) == identity
    assert isinstance(payload.get("timestamp"), int)


def test_cached_read_does_not_include_token():
    identity = {"username": "cached-user", "ithinktank": {"account": "cached-user"}}
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with _patch_mcp_sync():
                try_handle_get(handler, parsed)

    handler.headers = {}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        try_handle_get(handler, parsed)
    payload = _json_payload(handler)
    assert _without_timestamp(payload) == identity
    assert "access_token" not in payload
    assert isinstance(payload.get("timestamp"), int)


def test_upstream_401_passthrough_and_clears_cache():
    identity = {"username": "will-clear"}
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer good-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with _patch_mcp_sync():
                try_handle_get(handler, parsed)

    detail = {"detail": "会话不存在或已超时，请重新登录。"}
    handler.headers = {"Authorization": "bearer bad"}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(401, detail),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(401)
    payload = _json_payload(handler)
    assert _without_timestamp(payload) == detail
    assert isinstance(payload.get("timestamp"), int)

    handler.headers = {}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        try_handle_get(handler, parsed)
    assert _json_payload(handler)["error"] == "not_registered"


def test_upstream_403_passthrough():
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer tok"}
    parsed = urlparse("/api/integration/webui_login")
    detail = {"detail": "无权查询其他用户身份。"}
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(403, detail),
        ):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(403)
    payload = _json_payload(handler)
    assert _without_timestamp(payload) == detail
    assert isinstance(payload.get("timestamp"), int)


def test_lookup_unreachable_returns_502(capsys):
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer tok"}
    parsed = urlparse("/api/integration/webui_login")
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
    assert isinstance(payload.get("timestamp"), int)
    err = capsys.readouterr().err
    login_lines = [ln for ln in err.splitlines() if "[webui][integration_login][exit]" in ln]
    assert login_lines, err
    log_line = login_lines[0]
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", log_line)
    assert "status=502" in log_line
    assert "[webui] {" not in log_line


def test_expired_cache_returns_session_expired():
    identity = {"username": "expired-user"}
    now = 1_700_000_000.0
    with patch("integration.identity.session_store.time.time", return_value=now):
        with patch(
            "integration.identity.session_store.zhiling_identity_cache_ttl_seconds",
            return_value=30,
        ):
            from integration.identity.session_store import save_session

            save_session("plain-token", identity)

    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch("integration.identity.session_store.time.time", return_value=now + 31):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(401)
    payload = _json_payload(handler)
    assert payload["error"] == "session_expired"
    assert isinstance(payload.get("timestamp"), int)
