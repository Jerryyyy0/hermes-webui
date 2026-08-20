import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.knowledge_base import client
from integration.knowledge_base.client import KnowledgeBaseUpstreamError
from integration.knowledge_base.constants import DOWNSTREAM_ROUTE_NAMES, WEBUI_ROUTE_PREFIX
from integration.knowledge_base.handlers import _route_name, try_handle_post, try_handle_post_early


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


@pytest.mark.parametrize("route_name", sorted(DOWNSTREAM_ROUTE_NAMES))
def test_downstream_named_routes_resolve(route_name):
    parsed = urlparse(f"{WEBUI_ROUTE_PREFIX}{route_name}")

    assert _route_name(parsed) == route_name


@pytest.mark.parametrize(
    "legacy_route",
    ["list", "joined", "create", "info", "edit", "delete", "available", "apply_join", "members", "documents"],
)
def test_legacy_route_aliases_are_rejected(legacy_route):
    parsed = urlparse(f"{WEBUI_ROUTE_PREFIX}{legacy_route}")

    assert _route_name(parsed) is None


def test_upload_artifacts_route_resolves():
    assert _route_name(urlparse(f"{WEBUI_ROUTE_PREFIX}upload_artifacts")) == "upload_artifacts"


def test_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/list_ps_knowledge_bases")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=False):
        assert try_handle_post(handler, parsed, {}) is False


@pytest.mark.parametrize("route_name", sorted(DOWNSTREAM_ROUTE_NAMES))
def test_json_routes_forward_body_verbatim(route_name):
    if route_name in {"upload_docs", "show_pdf", "download_doc"}:
        pytest.skip("handled by dedicated raw or binary paths")

    handler = MagicMock()
    parsed = urlparse(f"{WEBUI_ROUTE_PREFIX}{route_name}")
    body = {"downstream_only": {"route": route_name}, "nullable": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, {"code": 200}),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True

    mock_post.assert_called_once_with(route_name, body)


def test_upstream_business_error():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_ps_kb_info")
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


def test_upstream_http_200_business_error_is_returned_unchanged():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_ps_kb_info")
    upstream = {"code": 500, "msg": "知识库不存在", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream),
        ):
            assert try_handle_post(handler, parsed, {"kbName": "kb_missing"}) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream


def test_upstream_missing_response_returns_500_with_chinese_message():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_ps_kb_info")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            side_effect=KnowledgeBaseUpstreamError("Connection refused"),
        ):
            assert try_handle_post(handler, parsed, {"kbName": "kb1"}) is True
    handler.send_response.assert_called_with(500)
    assert _json_payload(handler) == {
        "error": "知识库服务异常",
        "message": "知识库服务异常，请稍后重试",
    }


def test_show_pdf_success():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_pdf")
    body = {
        "kbName": "share54",
        "fileName": "1656号附件-电力中长期市场基本规则.pdf",
        "doc_id": "1961438302427021314",
        "downstream_option": {"prefer_cache": True},
    }
    upstream_data = {"url": "http://kb.test/preview/abc.pdf"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_binary_or_json",
            return_value=client.KnowledgeBaseShowPdfResult(
                kind="json", status=200, payload=upstream_data
            ),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_data
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "show_pdf"
    assert upstream_body == body


def test_show_pdf_binary():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/show_pdf")
    body = {"kbName": "share68", "fileName": "doc.pdf"}
    pdf_bytes = b"%PDF-1.4 test"
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_binary_or_json",
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
            "integration.knowledge_base.handlers.client.post_binary_or_json",
            return_value=client.KnowledgeBaseShowPdfResult(kind="json", status=200, payload=None),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "show_pdf"
    assert upstream_body["flag"] is True


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
    mock_post.assert_called_once_with("search_docs", body)


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
    mock_post.assert_called_once_with("search_docs_xcore", body)


def test_upload_docs_passthrough_raw_body():
    handler = MagicMock()
    content_type = 'multipart/form-data; boundary="----boundary"'
    raw_body = (
        b'--boundary\r\n'
        b'Content-Disposition: form-data; name="uuid"\r\n\r\n'
        b'uuid-1\r\n'
        b'--boundary--\r\n'
    )
    handler.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(raw_body)),
    }
    handler.rfile = BytesIO(raw_body)
    parsed = urlparse("/api/integration/knowledge_base/upload_docs")
    upstream_resp = {"code": "200", "msg": "success", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_raw_body",
            return_value=(200, upstream_resp),
        ) as mock_upload:
            assert try_handle_post_early(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_upload.assert_called_once_with(
        "upload_docs",
        body=raw_body,
        content_type=content_type,
    )


def test_upload_docs_rejects_invalid_content_length():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": 'multipart/form-data; boundary="----boundary"',
        "Content-Length": "not-a-number",
    }
    handler.rfile = BytesIO(b"")
    parsed = urlparse("/api/integration/knowledge_base/upload_docs")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post_early(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "Content-Length 无效"


def test_upload_docs_rejects_incomplete_body():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": 'multipart/form-data; boundary="----boundary"',
        "Content-Length": "10",
    }
    handler.rfile = BytesIO(b"short")
    parsed = urlparse("/api/integration/knowledge_base/upload_docs")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post_early(handler, parsed) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "请求体不完整"


def test_handle_application_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/creater_handle_application")
    body = {"userId": "u1", "uuid": "u1", "kbName": "share1", "action": "approve"}
    upstream_resp = {"code": 200, "msg": "操作成功"}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "creater_handle_application"
    assert upstream_body == body


def test_get_joinkb_applications_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/get_joinkb_applications")
    body = {
        "userId": "admin",
        "uuid": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
        "kbName": "share49",
    }
    upstream_resp = {
        "code": 200,
        "msg": "success",
        "data": [
            {
                "id": 120,
                "UserId": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
                "massage": "用户 wzq (wzq) 申请加入您的 法律法规 知识库",
                "targetKbName": "share49",
                "targetUserId": "7ef13dc63648f73a5aba7cab6432bb",
                "createTime": "2026-07-01T19:20:06.196674",
                "username": "wzq",
                "portraitType": "pt1",
            }
        ],
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "get_joinkb_applications"
    assert upstream_body == body


def test_get_user_messages_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/get_user_messages")
    body = {"account": "18810008888", "uuid": "u1", "readType": "unread"}
    upstream_resp = {"code": 200, "msg": "查询成功", "data": []}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "get_user_messages"
    assert upstream_body == body


def test_mark_message_read_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/mark_message_read")
    body = {"messageId": [112]}
    upstream_resp = {"code": 200, "msg": "操作成功", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "mark_message_read"
    assert upstream_body == body


def test_user_exit_shkb_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/user_exit_shkb")
    body = {
        "account": "wzq",
        "uuid": "7ef13dc63648f73a5aba7cab6432bb",
        "kbName": "share28",
    }
    upstream_resp = {
        "code": 200,
        "msg": "用户 wzq (wzq) 已成功主动退出知识库 人工智能会议纪要",
        "data": None,
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "user_exit_shkb"
    assert upstream_body == body


def test_remove_from_myshkb_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/remove_from_myshkb")
    body = {
        "account": ["wzq"],
        "uuid": ["7ef13dc63648f73a5aba7cab6432bb"],
        "kbName": "share20",
    }
    upstream_resp = {
        "code": 200,
        "msg": "用户 7ef13dc63648f73a5aba7cab6432bb 已被成功移出知识库 share20",
        "data": None,
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "remove_from_myshkb"
    assert upstream_body == body


def test_delete_readed_message_passthrough():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/delete_readed_message")
    body = {"messageId": [116, 1]}
    upstream_resp = {"code": 200, "msg": "操作成功", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_json",
            return_value=(200, upstream_resp),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once()
    route_key, upstream_body = mock_post.call_args.args
    assert route_key == "delete_readed_message"
    assert upstream_body == body


def test_download_doc_passthrough_binary():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/download_doc")
    body = {"knowledge_base_name": "share28", "file_name": "test1.pdf"}
    pdf_bytes = b"%PDF-1.4 download"
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_binary_or_json",
            return_value=client.KnowledgeBaseShowPdfResult(
                kind="binary",
                status=200,
                content=pdf_bytes,
                content_type="application/pdf",
                extra_headers={"Content-Disposition": 'attachment; filename="test1.pdf"'},
            ),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert handler.wfile.write.call_args.args[0] == pdf_bytes
    mock_post.assert_called_once_with("download_doc", body)


def test_download_doc_passthrough_json_error():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/download_doc")
    body = {"knowledge_base_name": "share28", "file_name": "missing.pdf"}
    upstream_resp = {"code": 500, "msg": "文件不存在", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch(
            "integration.knowledge_base.handlers.client.post_binary_or_json",
            return_value=client.KnowledgeBaseShowPdfResult(
                kind="json", status=500, payload=upstream_resp
            ),
        ) as mock_post:
            assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(500)
    assert _json_payload(handler) == upstream_resp
    mock_post.assert_called_once_with("download_doc", body)


def test_upload_artifacts_missing_uuid():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {
        "kbName": "share54",
        "fileProperties": [{"fileName": "a.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}],
        "paths": ["a.md"],
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "缺少用户 UUID"


def test_upload_artifacts_missing_file_properties():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {"uuid": "u1", "kbName": "share54", "paths": ["a.md"]}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "缺少文件属性"


def test_upload_artifacts_missing_paths():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {
        "uuid": "u1",
        "kbName": "share54",
        "fileProperties": [{"fileName": "a.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}],
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "缺少路径"


def test_upload_artifacts_count_mismatch():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {
        "uuid": "u1",
        "kbName": "share54",
        "fileProperties": [{"fileName": "a.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}],
        "paths": ["a.md", "b.md"],
    }
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "文件属性与路径数量不一致"


def test_upload_artifacts_path_traversal():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {
        "uuid": "u1",
        "kbName": "share54",
        "fileProperties": [{"fileName": "evil.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}],
        "paths": ["../etc/passwd"],
    }
    fake_ws = Path("/tmp/fake-workspace")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.workspace.resolve_trusted_workspace", return_value=fake_ws):
            with patch("api.workspace.safe_resolve_ws", side_effect=ValueError("traversal")):
                assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "路径越界"


def test_upload_artifacts_file_not_found():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    body = {
        "uuid": "u1",
        "kbName": "share54",
        "fileProperties": [{"fileName": "missing.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}],
        "paths": ["missing.md"],
    }
    fake_ws = Path("/tmp/fake-workspace")
    fake_resolved = Path("/tmp/fake-workspace/missing.md")
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.workspace.resolve_trusted_workspace", return_value=fake_ws):
            with patch("api.workspace.safe_resolve_ws", return_value=fake_resolved):
                assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "文件不存在"


def test_upload_artifacts_too_many():
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    paths = [f"file_{i}.md" for i in range(21)]
    file_props = [
        {"fileName": f"file_{i}.md", "fileClass": "直属", "fileUploader": "u1", "publicationDate": "1"}
        for i in range(21)
    ]
    body = {"uuid": "u1", "kbName": "share54", "fileProperties": file_props, "paths": paths}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(400)
    assert _json_payload(handler)["error"] == "文件数量过多"


def test_upload_artifacts_success(tmp_path):
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "2025.md").write_bytes(b"# report")
    (tmp_path / "summary.pdf").write_bytes(b"%PDF-1.4")
    file_props = [
        {"fileName": "2025.md", "fileClass": "直属", "fileUploader": "uuid-1", "publicationDate": "1"},
        {"fileName": "summary.pdf", "fileClass": "直属", "fileUploader": "uuid-1", "publicationDate": "1"},
    ]
    body = {
        "uuid": "uuid-1",
        "kbName": "share54",
        "fileProperties": file_props,
        "paths": ["reports/2025.md", "summary.pdf"],
    }
    upstream_resp = {"code": "200", "msg": "success", "data": None}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.workspace.resolve_trusted_workspace", return_value=tmp_path):
            with patch("api.workspace.safe_resolve_ws", side_effect=lambda ws, p: (ws / p).resolve()):
                with patch(
                    "integration.knowledge_base.handlers.client.post_multipart",
                    return_value=(200, upstream_resp),
                ) as mock_upload:
                    assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(200)
    assert _json_payload(handler) == upstream_resp
    mock_upload.assert_called_once()
    route_key = mock_upload.call_args.args[0]
    assert route_key == "upload_docs"
    kwargs = mock_upload.call_args.kwargs
    assert kwargs["data"]["kbName"] == "share54"
    fp_json = json.loads(kwargs["data"]["fileProperties"])
    assert fp_json == file_props
    assert len(kwargs["files"]) == 2
    assert kwargs["files"][0][1][0] == "2025.md"
    assert kwargs["files"][0][1][1] == b"# report"
    assert kwargs["files"][1][1][0] == "summary.pdf"
    assert kwargs["files"][1][1][1] == b"%PDF-1.4"


def test_upload_artifacts_upstream_error(tmp_path):
    handler = MagicMock()
    parsed = urlparse("/api/integration/knowledge_base/upload_artifacts")
    (tmp_path / "doc.md").write_bytes(b"content")
    file_props = [{"fileName": "doc.md", "fileClass": "直属", "fileUploader": "uuid-1", "publicationDate": "1"}]
    body = {"uuid": "uuid-1", "kbName": "share54", "fileProperties": file_props, "paths": ["doc.md"]}
    with patch("integration.knowledge_base.handlers.knowledge_base_enabled", return_value=True):
        with patch("api.workspace.resolve_trusted_workspace", return_value=tmp_path):
            with patch("api.workspace.safe_resolve_ws", side_effect=lambda ws, p: (ws / p).resolve()):
                with patch(
                    "integration.knowledge_base.handlers.client.post_multipart",
                    side_effect=KnowledgeBaseUpstreamError("connection refused"),
                ):
                    assert try_handle_post(handler, parsed, body) is True
    handler.send_response.assert_called_with(500)
    assert _json_payload(handler) == {
        "error": "知识库服务异常",
        "message": "知识库服务异常，请稍后重试",
    }
