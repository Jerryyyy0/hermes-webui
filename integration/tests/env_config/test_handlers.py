"""Tests for GET /api/integration/config."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.env_config.handlers import try_handle_get


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def _handler():
    return MagicMock()


def test_disabled_returns_false():
    handler = _handler()
    parsed = urlparse("/api/integration/config")
    with patch("integration.env_config.handlers.integration_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False


def test_unknown_path_returns_false():
    handler = _handler()
    parsed = urlparse("/api/integration/config/other")
    with patch("integration.env_config.handlers.integration_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is False


def test_returns_browser_preview_url():
    handler = _handler()
    parsed = urlparse("/api/integration/config")
    with patch("integration.env_config.handlers.integration_enabled", return_value=True):
        with patch.dict(
            os.environ,
            {"BROWSER_PREVIEW_URL": "http://preview.test/"},
            clear=False,
        ):
            assert try_handle_get(handler, parsed) is True

    assert _json_payload(handler) == {
        "browser_preview_url": "http://preview.test/"
    }
    handler.send_response.assert_called_with(200)


def test_preserves_explicit_empty_value():
    handler = _handler()
    parsed = urlparse("/api/integration/config")
    with patch("integration.env_config.handlers.integration_enabled", return_value=True):
        with patch.dict(os.environ, {"BROWSER_PREVIEW_URL": ""}, clear=False):
            assert try_handle_get(handler, parsed) is True

    assert _json_payload(handler) == {"browser_preview_url": ""}
    handler.send_response.assert_called_with(200)


def test_missing_variable_returns_server_error():
    handler = _handler()
    parsed = urlparse("/api/integration/config")
    with patch("integration.env_config.handlers.integration_enabled", return_value=True):
        with patch.dict(os.environ, {}, clear=True):
            assert try_handle_get(handler, parsed) is True

    assert _json_payload(handler) == {
        "error": "BROWSER_PREVIEW_URL 未配置"
    }
    handler.send_response.assert_called_with(500)
