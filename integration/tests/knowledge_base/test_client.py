import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from integration.knowledge_base import client


def test_parse_upstream_response_success():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    wrapper = {"code": 200, "msg": "success", "data": [{"kbName": "kb1"}]}
    resp.json.return_value = wrapper
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body == wrapper


def test_parse_upstream_response_success_string_code():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    wrapper = {"code": "200", "msg": "success", "data": None}
    resp.json.return_value = wrapper
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body == wrapper


def test_parse_upstream_response_business_error():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 500
    wrapper = {"code": 500, "msg": "failed", "data": None}
    resp.json.return_value = wrapper
    status, body = client.parse_upstream_response(resp)
    assert status == 500
    assert body == wrapper


def test_parse_upstream_response_direct_payload():
    """Downstream returns data without {code, msg, data} wrapper."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    payload = {"total": 7, "data": [{"id": 1}]}
    resp.json.return_value = payload
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body == payload


def test_parse_upstream_response_direct_array():
    """Downstream returns a JSON array (e.g. search_docs chunks)."""
    chunks = [
        {
            "page_content": "slice text",
            "metadata": {"source": "knowledge_base/share46/content/a.pdf", "file_name": "a.pdf"},
        }
    ]
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = chunks
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body == chunks


def test_parse_upstream_response_invalid_json():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 502
    resp.json.side_effect = ValueError("not json")
    status, body = client.parse_upstream_response(resp)
    assert status == 502
    assert body["error"] == "knowledge_base_upstream_failed"


def test_build_create_payload_fixed_values():
    body = {
        "account": "admin",
        "uuid": "uuid-1",
        "showName": "My KB",
        "isPersonal": 1,
    }
    payload = client.build_create_payload(body)
    assert payload["vsType"] == "faiss"
    assert payload["embedModel"] == "bce-base"
    assert payload["iconType"] == 1
    assert payload["location"] == "101"


def test_build_edit_payload_location_number():
    body = {"kbName": "kb1", "showName": "Renamed"}
    payload = client.build_edit_payload(body)
    assert payload["location"] == 101
    assert isinstance(payload["location"], int)


def test_build_search_docs_payload_defaults():
    body = {"query": "党建知识库", "kbName": "share46"}
    payload = client.build_search_docs_payload(body)
    assert payload == {
        "query": "党建知识库",
        "knowledge_base_name": "share46",
        "top_k": 3,
        "score_threshold": 1.0,
    }


def test_build_search_docs_payload_overrides():
    body = {
        "query": "党建知识库",
        "kbName": "share46",
        "topK": 5,
        "scoreThreshold": 0.5,
    }
    payload = client.build_search_docs_payload(body)
    assert payload["top_k"] == 5
    assert payload["score_threshold"] == 0.5


def test_build_search_docs_xcore_payload_defaults():
    body = {"query": "电力交易", "kbNames": ["share15"]}
    payload = client.build_search_docs_xcore_payload(body)
    assert payload == {
        "query": "电力交易",
        "kbNames": ["share15"],
        "top_k": 3,
        "score_threshold": 1.0,
    }


def test_build_search_docs_xcore_payload_overrides():
    body = {
        "query": "电力交易",
        "kbNames": ["share15", "share20"],
        "topK": 5,
        "scoreThreshold": 0.5,
    }
    payload = client.build_search_docs_xcore_payload(body)
    assert payload["kbNames"] == ["share15", "share20"]
    assert payload["top_k"] == 5
    assert payload["score_threshold"] == 0.5


def test_build_show_pdf_payload_without_flag():
    body = {"kbName": "share54", "fileName": "1656号附件-电力中长期市场基本规则.pdf"}
    payload = client.build_show_pdf_payload(body)
    assert payload == {
        "kbName": "share54",
        "fileName": "1656号附件-电力中长期市场基本规则.pdf",
        "aes_key": "",
        "aes_nonce": "",
    }


def test_build_show_pdf_payload_with_flag():
    body = {
        "kbName": "share54",
        "fileName": "关于促进电网高质量发展的指导意见(发改能源〔2025〕1710 号).docx",
        "flag": True,
    }
    payload = client.build_show_pdf_payload(body)
    assert payload["kbName"] == "share54"
    assert payload["flag"] is True


def test_post_show_pdf_json_error():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.content = b'{"code":404,"msg":"not found","data":null}'
    resp.json.return_value = {"code": 404, "msg": "not found", "data": None}

    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = resp
            mock_client_cls.return_value = mock_client
            result = client.post_show_pdf({"kbName": "kb1", "fileName": "a.pdf", "aes_key": "", "aes_nonce": ""})

    assert result.kind == "json"
    assert result.status == 200
    assert result.payload == {"code": 404, "msg": "not found", "data": None}


def test_post_show_pdf_binary_pdf():
    pdf_bytes = b"%PDF-1.4 fake pdf content"
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {
        "content-type": "application/pdf",
        "content-disposition": 'inline; filename="doc.pdf"',
    }
    resp.content = pdf_bytes
    resp.json.side_effect = ValueError("not json")

    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = resp
            mock_client_cls.return_value = mock_client
            result = client.post_show_pdf({"kbName": "kb1", "fileName": "doc.pdf", "aes_key": "", "aes_nonce": ""})

    assert result.kind == "binary"
    assert result.status == 200
    assert result.content == pdf_bytes
    assert result.content_type == "application/pdf"
    assert result.extra_headers["Content-Disposition"] == 'inline; filename="doc.pdf"'


def test_post_json_upstream_unreachable():
    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client_cls.return_value = mock_client
            with pytest.raises(client.KnowledgeBaseUpstreamError):
                client.post_json("list", {"account": "a", "uuid": "u", "isPersonal": 1})


def test_post_raw_body_forwards_bytes():
    captured = {}

    def _fake_post(url, content=None, headers=None, **kwargs):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers or {}
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"code": "200", "msg": "ok", "data": None}
        return resp

    body = b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\n\r\nAAA\r\n--b--\r\n"
    content_type = "multipart/form-data; boundary=b"

    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client
            status, payload = client.post_raw_body("upload_docs", body=body, content_type=content_type)

    assert status == 200
    assert payload == {"code": "200", "msg": "ok", "data": None}
    assert captured["url"] == "http://kb.test/knowledge_base/upload_docs"
    assert captured["content"] == body
    assert captured["headers"]["Content-Type"] == content_type


def test_post_json_forwards_body():
    captured = {}

    def _fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"code": 200, "msg": "ok", "data": []}
        return resp

    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = _fake_post
            mock_client_cls.return_value = mock_client
            status, body = client.post_json(
                "list",
                {"account": "admin", "uuid": "uuid-1", "isPersonal": 1},
            )
    assert status == 200
    assert body == {"code": 200, "msg": "ok", "data": []}
    assert captured["url"] == "http://kb.test/knowledge_base/list_ps_knowledge_bases"
    assert captured["json"]["account"] == "admin"
    assert captured["json"]["uuid"] == "uuid-1"
