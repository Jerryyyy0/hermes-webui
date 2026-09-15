"""Tests for GET /api/integration/slash_commands."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from api.routes import handle_get
from integration.slash_commands.handlers import try_handle_get


PATH = "/api/integration/slash_commands"
EXPECTED_PAYLOAD = {
    "commands": [
        {
            "name": "compact",
            "description": "压缩当前会话上下文，减少后续模型调用携带的历史内容。",
            "args_hint": "[focus topic]",
        }
    ],
}


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def test_disabled_returns_false():
    handler = MagicMock()
    with patch("integration.slash_commands.handlers.integration_enabled", return_value=False):
        assert try_handle_get(handler, urlparse(PATH)) is False


def test_unknown_path_returns_false():
    handler = MagicMock()
    with patch("integration.slash_commands.handlers.integration_enabled", return_value=True):
        assert try_handle_get(handler, urlparse(f"{PATH}/other")) is False


def test_returns_compact_catalog():
    handler = MagicMock()
    handler.headers = {}

    with patch("integration.slash_commands.handlers.integration_enabled", return_value=True):
        assert try_handle_get(handler, urlparse(PATH)) is True

    assert _json_payload(handler) == EXPECTED_PAYLOAD
    handler.send_response.assert_called_once_with(200)


class _RouteHandler:
    def __init__(self):
        self.headers = {}
        self.wfile = BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


def test_api_routes_dispatches_catalog():
    handler = _RouteHandler()

    with patch("integration.slash_commands.handlers.integration_enabled", return_value=True):
        assert handle_get(handler, urlparse(PATH)) is True

    assert handler.status == 200
    assert json.loads(handler.wfile.getvalue()) == EXPECTED_PAYLOAD
    assert handler.response_headers["Content-Length"] == str(len(handler.wfile.getvalue()))
