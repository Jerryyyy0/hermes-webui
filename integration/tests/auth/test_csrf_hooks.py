"""Tests for Zhiling split WebUI CSRF bypass hook."""

import pytest

from integration.auth import csrf_hooks


@pytest.fixture(autouse=True)
def _reset_csrf_hook_installed():
    csrf_hooks._installed = False
    yield
    csrf_hooks._installed = False


def test_split_webui_csrf_bypass_requires_integration(monkeypatch):
    monkeypatch.delenv("HERMES_INTEGRATION", raising=False)
    handler = csrf_hooks._fake_handler(
        Origin="http://192.168.1.139:23003",
        Authorization="Bearer token",
    )
    assert not csrf_hooks.split_webui_csrf_bypass(handler)


def test_split_webui_csrf_bypass_allows_trusted_origin_with_auth(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    handler = csrf_hooks._fake_handler(
        Origin="http://192.168.1.139:23003/foo",
        Authorization="Bearer token",
    )
    assert csrf_hooks.split_webui_csrf_bypass(handler)


def test_split_webui_csrf_bypass_allows_forwarded_user(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    handler = csrf_hooks._fake_handler(
        Referer="http://47.93.211.132:23003/app",
        **{"X-Forwarded-User": "alice"},
    )
    assert csrf_hooks.split_webui_csrf_bypass(handler)


def test_split_webui_csrf_bypass_rejects_untrusted_origin(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    handler = csrf_hooks._fake_handler(
        Origin="http://evil.example:23003",
        Authorization="Bearer token",
    )
    assert not csrf_hooks.split_webui_csrf_bypass(handler)


def test_split_webui_csrf_bypass_rejects_without_proxy_identity(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    handler = csrf_hooks._fake_handler(Origin="http://192.168.1.139:23003")
    assert not csrf_hooks.split_webui_csrf_bypass(handler)


def test_install_hook_wraps_routes_check_csrf(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")

    import api.routes as routes

    calls = {"original": 0}

    def _original(handler):
        calls["original"] += 1
        return False

    monkeypatch.setattr(routes, "_check_csrf", _original)
    csrf_hooks.install_zhiling_split_webui_csrf_hook()

    trusted = csrf_hooks._fake_handler(
        Origin="http://192.168.1.139:23003",
        Authorization="Bearer token",
    )
    assert routes._check_csrf(trusted) is True
    assert calls["original"] == 0

    untrusted = csrf_hooks._fake_handler(Origin="http://localhost:8787")
    assert routes._check_csrf(untrusted) is False
    assert calls["original"] == 1
