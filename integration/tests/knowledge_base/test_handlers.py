import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.knowledge_base import client
from integration.knowledge_base.handlers import try_handle_post, try_handle_post_early


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


def test_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/list")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=False):
        assert try_handle_post(handler, parsed, {}) is False


def test_list_missing_account():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/list")
    body = {"uuid": "uuid-1", "isPersonal": 1}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_account"


def test_list_missing_uuid():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/list")
    body = {"account": "admin", "isPersonal": 1}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_uuid"


def test_list_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/list")
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
    parsed = urlparse("/api/integration/knowledge_base/create")
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
    parsed = urlparse("/api/integration/knowledge_base/edit")
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
    parsed = urlparse("/api/integration/knowledge_base/info")
    body = {"kbName": "kb_missing"}
    upstream = {"code": 500, "msg": "not found", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(500, upstream),
        ):
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(500)
    assert _json_payload(handler) == upstream


def test_show_pdf_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_pdf")
    body = {
        "kbName": "share54",
        "fileName": "1656号附件-电力中长期市场基本规则.pdf",
    }
    upstream_data = {"url": "http://kb.test/preview/abc.pdf"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_show_pdf",
            return_value=client.KnowledgeBaseShowPdfResult(
                kind="json", status=200, payload=upstream_data
            ),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_data
    mock_post.assert_called_once()
    upstream_body = mock_post.call_args.args[0]
    assert upstream_body == {
        "kbName": "share54",
        "fileName": "1656号附件-电力中长期市场基本规则.pdf",
        "aes_key": "",
        "aes_nonce": "",
    }


def test_show_pdf_binary():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_pdf")
    body = {"kbName": "share68", "fileName": "doc.pdf"}
    pdf_bytes = b"%PDF-1.4 test"
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_show_pdf",
            return_value=client.KnowledgeBaseShowPdfResult(
                kind="binary",
                status=200,
                content=pdf_bytes,
                content_type="application/pdf",
                extra_headers={"Content-Disposition": 'inline; filename="doc.pdf"'},
            ),
        ):
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert handler.send_header.call_args_list[0].args == ("Content-Type", "application/pdf")
    handler.wfile.write.assert_called_once_with(pdf_bytes)


def test_show_pdf_with_flag():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_pdf")
    body = {
        "kbName": "share54",
        "fileName": "关于促进电网高质量发展的指导意见(发改能源〔2025〕1710 号).docx",
        "flag": True,
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_show_pdf",
            return_value=client.KnowledgeBaseShowPdfResult(kind="json", status=200, payload=None),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    upstream_body = mock_post.call_args.args[0]
    assert upstream_body["flag"] is True


def test_search_docs_missing_query():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/search_docs")
    body = {"kbName": "share46"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_query"


def test_search_docs_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/search_docs")
    body = {
        "query": "党建知识库",
        "kbName": "share46",
        "topK": 5,
        "scoreThreshold": 0.5,
    }
    upstream_data = [
        {
            "page_content": "文本切片内容...",
            "metadata": {
                "source": "knowledge_base/share20/content/文件名称.pdf",
                "file_name": "文件名称.pdf",
            },
        }
    ]
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_data),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_data
    mock_post.assert_called_once_with(
        "search_docs",
        {
            "query": "党建知识库",
            "knowledge_base_name": "share46",
            "top_k": 5,
            "score_threshold": 0.5,
        },
    )


def test_search_docs_xcore_missing_kb_names():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/search_docs_xcore")
    body = {"query": "电力交易"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_kbNames"


def test_search_docs_xcore_empty_kb_names():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/search_docs_xcore")
    body = {"query": "电力交易", "kbNames": []}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "missing_kbNames"


def test_search_docs_xcore_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/search_docs_xcore")
    body = {
        "query": "2025年1月电力交易成交电量是多少",
        "kbNames": ["share15"],
        "topK": 3,
        "scoreThreshold": 1,
    }
    upstream_data = {
        "kbNames": ["share15"],
        "docNames": ["2025年1月电力交易统计月报.pdf"],
        "context": ["总交易电量完成5541亿千瓦时..."],
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_data),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_data
    mock_post.assert_called_once_with(
        "search_docs_xcore",
        {
            "query": "2025年1月电力交易成交电量是多少",
            "kbNames": ["share15"],
            "top_k": 3,
            "score_threshold": 1,
        },
    )


def test_upload_docs_missing_uuid():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": 'multipart/form-data; boundary="----boundary"',
        "Content-Length": "10",
    }
    handler.rfile = BytesIO(b"")
    parsed = urlparse("/api/integration/knowledge_base/upload-docs")
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
    parsed = urlparse("/api/integration/knowledge_base/upload-docs")
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
    assert _json_payload(handler) is None
    mock_upload.assert_called_once()
    _, kwargs = mock_upload.call_args
    assert kwargs["data"]["kbName"] == "kb1"
    assert kwargs["data"]["fileProperties"] == file_props
    assert kwargs["files"][0][1][0] == "doc.pdf"
