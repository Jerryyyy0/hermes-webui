import json
import uuid
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.record_scripts import store
from integration.record_scripts.handlers import try_handle_get, try_handle_post, try_handle_post_early


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def _handler() -> MagicMock:
    h = MagicMock()
    h.headers = _Headers()
    return h


@pytest.fixture
def root(tmp_path):
    return tmp_path / "attachments" / "record_scripts"


@pytest.fixture
def enabled(root):
    with patch("integration.record_scripts.handlers.integration_enabled", return_value=True):
        with patch("integration.record_scripts.store.record_scripts_root", return_value=root.resolve()):
            yield


def _multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    body = b""
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += value.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: text/csv\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def test_disabled_returns_false():
    h = _handler()
    with patch("integration.record_scripts.handlers.integration_enabled", return_value=False):
        assert try_handle_get(h, urlparse("/api/integration/record_scripts/list")) is False
        assert try_handle_post(h, urlparse("/api/integration/record_scripts/save"), {}) is False
        assert try_handle_post_early(h, urlparse("/api/integration/record_scripts_csv/upload")) is False


def test_save_list_update_delete_script_string(enabled, root):
    h = _handler()
    parsed = urlparse("/api/integration/record_scripts/save")
    assert try_handle_post(h, parsed, {"relate_name": "flow_1", "script_json": "{\"a\":1}"}) is True
    payload = _json_payload(h)
    assert payload["ok"] is True
    assert payload["relate_name"] == "flow_1"
    assert payload["script_json"] == "{\"a\":1}"
    assert (root / "flow_1" / "script.json").read_text(encoding="utf-8") == "{\"a\":1}"

    h = _handler()
    assert try_handle_get(h, urlparse("/api/integration/record_scripts/list")) is True
    payload = _json_payload(h)
    assert payload["scripts"][0]["relate_name"] == "flow_1"
    assert payload["scripts"][0]["script_json"] == "{\"a\":1}"
    assert payload["scripts"][0]["has_csv"] is False

    h = _handler()
    assert try_handle_post(
        h,
        urlparse("/api/integration/record_scripts/update"),
        {"relate_name": "flow_1", "script_json": "{\"a\":2}"},
    ) is True
    assert _json_payload(h)["script_json"] == "{\"a\":2}"
    assert (root / "flow_1" / "script.json").read_text(encoding="utf-8") == "{\"a\":2}"

    (root / "flow_1" / "data.csv").write_bytes(b"a,b\n")
    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts/delete"), {"relate_name": "flow_1"}) is True
    assert _json_payload(h) == {"ok": True, "relate_name": "flow_1"}
    assert not (root / "flow_1" / "script.json").exists()
    assert (root / "flow_1" / "data.csv").read_bytes() == b"a,b\n"


def test_save_rejects_duplicate_and_invalid_name(enabled):
    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts/save"), {"relate_name": "flow", "script_json": "{}"}) is True
    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts/save"), {"relate_name": "flow", "script_json": "{}"}) is True
    h.send_response.assert_called_with(400)

    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts/save"), {"relate_name": "../x", "script_json": "{}"}) is True
    h.send_response.assert_called_with(400)


def test_script_json_must_be_string(enabled):
    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts/save"), {"relate_name": "flow", "script_json": {}}) is True
    h.send_response.assert_called_with(400)
    assert _json_payload(h)["error"] == "script_json 必须是字符串"


def test_csv_upload_download_delete(enabled, root):
    store.create_script("flow_csv", "{}")
    content_type, body = _multipart({"relate_name": "flow_csv"}, "data.csv", b"a,b\n1,2\n")
    h = _handler()
    h.headers = _Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    h.rfile = BytesIO(body)

    assert try_handle_post_early(h, urlparse("/api/integration/record_scripts_csv/upload")) is True
    payload = _json_payload(h)
    assert payload["ok"] is True
    assert payload["relate_name"] == "flow_csv"
    assert (root / "flow_csv" / "data.csv").read_bytes() == b"a,b\n1,2\n"

    h = _handler()
    assert try_handle_get(h, urlparse("/api/integration/record_scripts_csv/download?relate_name=flow_csv")) is True
    h.send_response.assert_called_with(200)
    assert h.wfile.write.call_args.args[0] == b"a,b\n1,2\n"

    h = _handler()
    assert try_handle_post(h, urlparse("/api/integration/record_scripts_csv/delete"), {"relate_name": "flow_csv"}) is True
    assert _json_payload(h) == {"ok": True, "relate_name": "flow_csv"}
    assert not (root / "flow_csv" / "data.csv").exists()


def test_csv_upload_requires_existing_script_and_csv_extension(enabled):
    content_type, body = _multipart({"relate_name": "missing"}, "data.csv", b"a\n")
    h = _handler()
    h.headers = _Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    h.rfile = BytesIO(body)
    assert try_handle_post_early(h, urlparse("/api/integration/record_scripts_csv/upload")) is True
    h.send_response.assert_called_with(404)

    store.create_script("flow_csv", "{}")
    content_type, body = _multipart({"relate_name": "flow_csv"}, "data.txt", b"a\n")
    h = _handler()
    h.headers = _Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    h.rfile = BytesIO(body)
    assert try_handle_post_early(h, urlparse("/api/integration/record_scripts_csv/upload")) is True
    h.send_response.assert_called_with(400)
    assert _json_payload(h)["error"] == "仅支持 CSV 文件"
