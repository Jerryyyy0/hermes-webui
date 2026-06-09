import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.knowledge_base.handlers import try_handle_post, try_handle_post_early


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def test_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/list")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=False):
        assert try_handle_post(handler, parsed, {}) is False


def test_list_missing_account():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/list")
    body = {"uuid": "uuid-1", "isPersonal": 1}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_account"


def test_list_missing_uuid():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/list")
    body = {"account": "admin", "isPersonal": 1}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_uuid"


def test_list_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/list")
    body = {"account": "admin", "uuid": "uuid-1", "isPersonal": 1}
    upstream_data = [{"kbName": "kb1", "showName": "KB 1"}]
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_data),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_data
    mock_post.assert_called_once()
    upstream_body = mock_post.call_args.args[1]
    assert upstream_body["account"] == "admin"
    assert upstream_body["uuid"] == "uuid-1"


def test_create_injects_fixed_values():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/create")
    body = {
        "account": "admin",
        "uuid": "uuid-1",
        "showName": "New KB",
        "isPersonal": 0,
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, None),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    upstream_body = mock_post.call_args.args[1]
    assert upstream_body["vsType"] == "faiss"
    assert upstream_body["location"] == "101"


def test_edit_location_number():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/edit")
    body = {"kbName": "kb1", "showName": "Renamed"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, None),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    upstream_body = mock_post.call_args.args[1]
    assert upstream_body["location"] == 101


def test_upstream_business_error():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge-base/info")
    body = {"kbName": "kb_missing"}
    err = {"error": "knowledge_base_upstream_error", "message": "not found", "code": 500}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(400, err),
        ):
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "knowledge_base_upstream_error"


def test_upload_docs_missing_uuid():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": 'multipart/form-data; boundary="----boundary"',
        "Content-Length": "10",
    }
    handler.rfile = BytesIO(b"")
    parsed = urlparse("/api/integration/knowledge-base/upload-docs")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.upload.parse_multipart") as mock_parse:
            mock_parse.return_value = (
                {"kbName": "kb1", "fileProperties": "[]"},
                {"files": ("doc.pdf", b"%PDF")},
            )
            assert try_handle_post_early(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_uuid"


def test_upload_docs_success():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": 'multipart/form-data; boundary="----boundary"',
        "Content-Length": "100",
    }
    handler.rfile = BytesIO(b"")
    parsed = urlparse("/api/integration/knowledge-base/upload-docs")
    file_props = json.dumps(
        [
            {
                "fileName": "doc.pdf",
                "fileClass": "",
                "fileUploader": "uuid-1",
                "publicationDate": "1",
            }
        ]
    )
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.upload.parse_multipart") as mock_parse:
            mock_parse.return_value = (
                {
                    "kbName": "kb1",
                    "uuid": "uuid-1",
                    "fileProperties": file_props,
                },
                {"files": ("doc.pdf", b"%PDF-1.4")},
            )
            with patch(
                "integration.knowledge_base.handlers.client.post_multipart",
                return_value=(200, None),
            ) as mock_upload:
                assert try_handle_post_early(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == {"ok": True}
    mock_upload.assert_called_once()
    _, kwargs = mock_upload.call_args
    assert kwargs["data"]["kbName"] == "kb1"
    assert kwargs["data"]["fileProperties"] == file_props
    assert kwargs["files"][0][1][0] == "doc.pdf"
