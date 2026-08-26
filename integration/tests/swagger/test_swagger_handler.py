"""Swagger handler: dynamic servers and offline asset references."""

import json
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from integration.swagger import swagger_handler as sh


def _handler(**headers):
    h = MagicMock()
    h.headers = headers
    return h


def test_public_base_url_from_forwarded_headers():
    url = sh._public_base_url(
        _handler(
            Host="internal:8787",
            **{"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hermes.corp.example"},
        )
    )
    assert url == "https://hermes.corp.example"


@patch("api.auth._is_secure_context", return_value=False)
def test_public_base_url_falls_back_to_host(_secure):
    url = sh._public_base_url(_handler(Host="10.0.0.5:8787"))
    assert url == "http://10.0.0.5:8787"


def test_public_base_url_missing_host_returns_none():
    assert sh._public_base_url(_handler()) is None


@patch("api.auth._is_secure_context", return_value=False)
def test_apply_dynamic_servers_uses_request_origin(_secure):
    spec = {"servers": [{"url": "/", "description": "placeholder"}]}
    sh._apply_dynamic_servers(spec, _handler(Host="api.internal"))
    assert spec["servers"] == [{"url": "http://api.internal", "description": "当前服务"}]


def test_apply_dynamic_servers_relative_when_no_host():
    spec = {}
    sh._apply_dynamic_servers(spec, _handler())
    assert spec["servers"] == [{"url": "/", "description": "当前站点"}]


def test_swagger_html_uses_local_assets_not_cdn():
    assert "cdn.jsdelivr.net" not in sh._SWAGGER_HTML
    assert "static/integration/swagger-ui/swagger-ui-bundle.js" in sh._SWAGGER_HTML
    assert 'url: "api/openapi.json"' in sh._SWAGGER_HTML


def test_swagger_html_uses_own_favicon_not_webui():
    assert "static/integration/swagger-ui/favicon.svg" in sh._SWAGGER_HTML
    assert "favicon.ico" not in sh._SWAGGER_HTML


@patch("integration.swagger.swagger_handler.open", new_callable=mock_open, read_data='{"openapi":"3.0.3","servers":[]}')
@patch("integration.swagger.swagger_handler.j")
def test_handle_openapi_json_injects_servers(mock_j, _mock_open):
    handler = _handler(Host="docs.test:9000", **{"X-Forwarded-Proto": "https"})
    sh.handle_openapi_json(handler)
    mock_j.assert_called_once()
    spec = mock_j.call_args[0][1]
    assert spec["servers"] == [{"url": "https://docs.test:9000", "description": "当前服务"}]


@patch("integration.swagger.swagger_handler.open", new_callable=mock_open, read_data='{"openapi":"3.0.3"}')
@patch("integration.swagger.swagger_handler.j")
def test_handle_openapi_json_valid_json(mock_j, _mock_open):
    sh.handle_openapi_json(_handler(Host="localhost"))
    spec = mock_j.call_args[0][1]
    json.dumps(spec)


def test_knowledge_base_passthrough_response_schemas_match_proxy_contract():
    spec_path = Path(sh.__file__).with_name("openapi.json")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    prefix = "/api/integration/knowledge_base/"
    passthrough_ref = "#/components/schemas/KnowledgeBasePassthroughResponse"

    for route_name in {
        "list_ps_knowledge_bases",
        "user_joined_shkbs",
        "create_ps_kb",
        "edit_kb_information",
        "delete_ps_kb",
        "available_shkbs",
        "apply_join_shkb",
        "get_user_inshkb",
        "list_knowledge_bases_details",
        "update_docs",
        "delete_docs",
    }:
        responses = spec["paths"][f"{prefix}{route_name}"]["post"]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"] == passthrough_ref
        assert responses["400"]["content"]["application/json"]["schema"]["$ref"] == passthrough_ref

    for route_name in {"upload_docs", "upload_artifacts"}:
        schema = spec["paths"][f"{prefix}{route_name}"]["post"]["responses"]["400"]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/KnowledgeBaseLocalOrPassthroughError"
