import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from integration.knowledge_base import client


def test_parse_upstream_response_success():
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"code": 200, "msg": "success", "data": [{"kbName": "kb1"}]}
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body == [{"kbName": "kb1"}]


def test_parse_upstream_response_success_string_code():
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"code": "200", "msg": "success", "data": None}
    status, body = client.parse_upstream_response(resp)
    assert status == 200
    assert body is None


def test_parse_upstream_response_business_error():
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"code": 500, "msg": "failed", "data": None}
    status, body = client.parse_upstream_response(resp)
    assert status == 400
    assert body["error"] == "knowledge_base_upstream_error"
    assert body["code"] == 500


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


def test_post_json_upstream_unreachable():
    with patch("integration.knowledge_base.client.knowledge_base_url", return_value="http://kb.test"):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            mock_client_cls.return_value = mock_client
            with pytest.raises(client.KnowledgeBaseUpstreamError):
                client.post_json("list", {"account": "a", "uuid": "u", "isPersonal": 1})


def test_post_json_forwards_body():
    captured = {}

    def _fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock(spec=httpx.Response)
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
    assert body == []
    assert captured["url"] == "http://kb.test/knowledge_base/list_ps_knowledge_bases"
    assert captured["json"]["account"] == "admin"
    assert captured["json"]["uuid"] == "uuid-1"
