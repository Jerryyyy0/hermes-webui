"""Tests for /api/integration/webui_appearance handlers."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.webui_appearance.handlers import try_handle_get
from integration.webui_appearance.paths import resolve_appearance_rel


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


@contextmanager
def _appearance_home(tmp_path):
    root = tmp_path / "webui-appearance"
    root.mkdir()
    with patch("integration.webui_appearance.paths.hermes_home", return_value=tmp_path):
        with patch("integration.webui_appearance.handlers.integration_enabled", return_value=True):
            yield root


def test_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/webui_appearance")
    with patch("integration.webui_appearance.handlers.integration_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False


def test_unknown_path_returns_false(tmp_path):
    handler = MagicMock()
    parsed = urlparse("/api/integration/webui_appearance/other")
    with _appearance_home(tmp_path):
        assert try_handle_get(handler, parsed) is False


def test_config_returns_json_as_is(tmp_path):
    payload = {
        "product": {"name": "李洋", "slogan": "智能协作工作台", "logo": "src/ly.jpg"},
        "chatBackground": {"mode": "solid", "color": "#9FA8DAFF", "opacity": 100, "image": ""},
        "theme": {"name": "国网绿", "primaryColorKey": "brand-9"},
    }
    with _appearance_home(tmp_path) as root:
        (root / "webui-appearance.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance")
        assert try_handle_get(handler, parsed) is True
    assert _json_payload(handler) == payload
    handler.send_response.assert_called_with(200)


def test_config_missing_file(tmp_path):
    with _appearance_home(tmp_path):
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(404)
    assert _json_payload(handler)["error"] == "外观配置文件不存在"


def test_config_invalid_json(tmp_path):
    with _appearance_home(tmp_path) as root:
        (root / "webui-appearance.json").write_text("{not-json", encoding="utf-8")
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert "合法 JSON" in _json_payload(handler)["error"]


def test_file_stream_invokes_serve(tmp_path):
    with _appearance_home(tmp_path) as root:
        src = root / "src"
        src.mkdir()
        image = src / "ly.jpg"
        image.write_bytes(b"\xff\xd8\xfffake")
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance/file?path=src/ly.jpg")
        with patch("api.routes._serve_file_bytes", return_value=True) as serve:
            assert try_handle_get(handler, parsed) is True
            serve.assert_called_once()
            args, kwargs = serve.call_args
            assert args[0] is handler
            assert args[1] == image.resolve()
            assert args[2] == "image/jpeg"
            assert args[3] is None
            assert args[4] == "no-store"
            assert kwargs.get("anchor_root") == root.resolve()


def test_file_missing_path_param(tmp_path):
    with _appearance_home(tmp_path):
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance/file")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "path 为必填参数"


def test_file_not_found(tmp_path):
    with _appearance_home(tmp_path):
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance/file?path=missing.jpg")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(404)
    assert _json_payload(handler)["error"] == "文件不存在"


def test_file_path_traversal_rejected(tmp_path):
    with _appearance_home(tmp_path):
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance/file?path=../secret.txt")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert "路径" in _json_payload(handler)["error"]


def test_file_absolute_path_rejected(tmp_path):
    with _appearance_home(tmp_path):
        handler = MagicMock()
        parsed = urlparse("/api/integration/webui_appearance/file?path=/etc/passwd")
        assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert "绝对路径" in _json_payload(handler)["error"]


def test_resolve_appearance_rel_ok(tmp_path):
    with patch("integration.webui_appearance.paths.hermes_home", return_value=tmp_path):
        root = tmp_path / "webui-appearance"
        root.mkdir()
        (root / "a.txt").write_text("x", encoding="utf-8")
        assert resolve_appearance_rel("a.txt") == (root / "a.txt").resolve()
