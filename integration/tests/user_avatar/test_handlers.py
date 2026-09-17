"""Tests for POST /api/upload?purpose=user_avatar."""

from __future__ import annotations

import json
import uuid
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.user_avatar.handlers import try_handle_upload
from integration.user_avatar import store as avatar_store
from integration.webui_appearance.handlers import try_handle_get


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _handler(content_type: str, body: bytes) -> MagicMock:
    handler = MagicMock()
    handler.headers = _Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    handler.rfile = BytesIO(body)
    return handler


def _multipart(filename: str, content: bytes) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def _payload(handler: MagicMock) -> dict:
    return json.loads(handler.wfile.write.call_args.args[0].decode())


def _enabled_avatar_home(tmp_path):
    return (
        patch("integration.user_avatar.handlers.integration_enabled", return_value=True),
        patch("integration.webui_appearance.paths.hermes_home", return_value=tmp_path),
    )


def test_unrelated_upload_purpose_is_not_consumed():
    content_type, body = _multipart("avatar.png", b"\x89PNG\r\n\x1a\n")
    handler = _handler(content_type, body)

    assert try_handle_upload(handler, urlparse("/api/upload")) is False
    assert handler.rfile.tell() == 0


def test_post_router_delegates_avatar_variant_before_generic_upload(tmp_path):
    from api import routes

    content_type, body = _multipart("avatar.png", b"\x89PNG\r\n\x1a\nrouter")
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)
    with (
        enabled,
        home,
        patch("api.routes._check_csrf", return_value=True),
        patch("api.routes.RequestDiagnostics.maybe_start", return_value=None),
        patch("api.routes.handle_upload") as generic_upload,
    ):
        assert routes.handle_post(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    generic_upload.assert_not_called()
    assert _payload(handler) == {"ok": True}


def test_avatar_upload_writes_configured_preview_url(tmp_path):
    content_type, body = _multipart("avatar.png", b"\x89PNG\r\n\x1a\nfake")
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home:
        assert try_handle_upload(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    assert _payload(handler) == {"ok": True}
    config = json.loads((tmp_path / "webui-appearance" / "webui-appearance.json").read_text())
    path = config["user_avatar_path"]
    assert path.startswith("user_avatar/avatar-")
    filename = path.split("/", 1)[1]
    assert (tmp_path / "webui-appearance" / "user_avatar" / filename).read_bytes() == b"\x89PNG\r\n\x1a\nfake"


def test_avatar_upload_preserves_existing_appearance_settings(tmp_path):
    root = tmp_path / "webui-appearance"
    root.mkdir()
    config_path = root / "webui-appearance.json"
    config_path.write_text(json.dumps({"theme": {"name": "green"}}), encoding="utf-8")
    content_type, body = _multipart("avatar.webp", b"RIFF\x00\x00\x00\x00WEBPfake")
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home:
        assert try_handle_upload(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    config = json.loads(config_path.read_text())
    assert config["theme"] == {"name": "green"}
    assert config["user_avatar_path"].endswith(".webp")


def test_appearance_query_returns_avatar_url_after_upload(tmp_path):
    content_type, body = _multipart("avatar.png", b"\x89PNG\r\n\x1a\nfake")
    upload_handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home:
        assert try_handle_upload(upload_handler, urlparse("/api/upload?purpose=user_avatar")) is True

    appearance_handler = MagicMock()
    with (
        patch("integration.webui_appearance.handlers.integration_enabled", return_value=True),
        patch("integration.webui_appearance.paths.hermes_home", return_value=tmp_path),
    ):
        assert try_handle_get(appearance_handler, urlparse("/api/integration/webui_appearance")) is True

    assert _payload(appearance_handler) == {
        "user_avatar_path": json.loads(
            (tmp_path / "webui-appearance" / "webui-appearance.json").read_text()
        )["user_avatar_path"]
    }


def test_replacing_avatar_updates_config_and_removes_previous_avatar(tmp_path):
    enabled, home = _enabled_avatar_home(tmp_path)
    first_type, first_body = _multipart("first.png", b"\x89PNG\r\n\x1a\nfirst")
    first_handler = _handler(first_type, first_body)
    with enabled, home:
        assert try_handle_upload(first_handler, urlparse("/api/upload?purpose=user_avatar")) is True

    config_path = tmp_path / "webui-appearance" / "webui-appearance.json"
    first_path = json.loads(config_path.read_text())["user_avatar_path"]
    first_file = tmp_path / "webui-appearance" / first_path
    assert first_file.exists()

    second_type, second_body = _multipart("second.jpg", b"\xff\xd8\xffsecond")
    second_handler = _handler(second_type, second_body)
    with enabled, home:
        assert try_handle_upload(second_handler, urlparse("/api/upload?purpose=user_avatar")) is True

    second_path = json.loads(config_path.read_text())["user_avatar_path"]
    assert second_path != first_path
    assert not first_file.exists()
    assert (tmp_path / "webui-appearance" / second_path).exists()


def test_config_write_failure_does_not_publish_or_leave_candidate_avatar(tmp_path):
    content_type, body = _multipart("avatar.png", b"\x89PNG\r\n\x1a\nfail")
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home, patch.object(avatar_store, "_write_json_atomically", side_effect=OSError("disk full")):
        assert try_handle_upload(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    assert handler.send_response.call_args.args[0] == 500
    assert _payload(handler)["error"] == "头像保存失败"
    avatar_dir = tmp_path / "webui-appearance" / "user_avatar"
    assert not list(avatar_dir.iterdir())
    assert not (tmp_path / "webui-appearance" / "webui-appearance.json").exists()


def test_avatar_upload_rejects_unsafe_svg_without_mutating_config(tmp_path):
    root = tmp_path / "webui-appearance"
    root.mkdir()
    config_path = root / "webui-appearance.json"
    config_path.write_text('{"theme":"green"}', encoding="utf-8")
    content_type, body = _multipart("avatar.svg", b'<svg><script>alert(1)</script></svg>')
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home:
        assert try_handle_upload(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    assert handler.send_response.call_args.args[0] == 400
    assert json.loads(config_path.read_text()) == {"theme": "green"}
    assert not (root / "user_avatar").exists()


def test_avatar_upload_rejects_array_appearance_config(tmp_path):
    root = tmp_path / "webui-appearance"
    root.mkdir()
    (root / "webui-appearance.json").write_text("[]", encoding="utf-8")
    content_type, body = _multipart("avatar.jpg", b"\xff\xd8\xfffake")
    handler = _handler(content_type, body)
    enabled, home = _enabled_avatar_home(tmp_path)

    with enabled, home:
        assert try_handle_upload(handler, urlparse("/api/upload?purpose=user_avatar")) is True

    assert handler.send_response.call_args.args[0] == 409
    assert _payload(handler)["error"] == "外观配置必须是 JSON 对象才能设置头像"
