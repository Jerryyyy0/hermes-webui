"""HTTP client for knowledge base downstream service."""

from __future__ import annotations

import json
from typing import Any

import httpx

from integration.config import knowledge_base_url
from integration.knowledge_base.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_LOCATION,
    DEFAULT_PAGE_SIZE,
    DELETE_CONTENT,
    EMBED_MODEL,
    ICON_TYPE,
    NOT_REFRESH_VS_CACHE,
    VS_TYPE,
    API_PREFIX,
    DOWNSTREAM_PATHS,
)

_TIMEOUT = 30.0
_UPLOAD_TIMEOUT = 120.0


class KnowledgeBaseUpstreamError(Exception):
    """Downstream unreachable or misconfigured."""


def _base_url() -> str:
    url = knowledge_base_url()
    if not url:
        raise KnowledgeBaseUpstreamError("KNOWLEDGE_BASE_URL not configured")
    return url


def _client(timeout: float = _TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _downstream_url(route_key: str) -> str:
    path = DOWNSTREAM_PATHS.get(route_key)
    if not path:
        raise ValueError(f"unknown route key: {route_key}")
    return f"{_base_url()}{API_PREFIX}/{path}"


def _is_success_code(code: Any) -> bool:
    return str(code) == "200"


def parse_upstream_response(resp: httpx.Response) -> tuple[int, Any]:
    """Map downstream {code, msg, data} to (http_status, body)."""
    try:
        payload = resp.json()
    except Exception:
        return 502, {
            "error": "knowledge_base_upstream_failed",
            "message": "invalid JSON from upstream",
        }

    if not isinstance(payload, dict):
        return 502, {
            "error": "knowledge_base_upstream_failed",
            "message": "unexpected upstream response shape",
        }

    code = payload.get("code")
    msg = payload.get("msg", "")
    data = payload.get("data")

    if _is_success_code(code):
        return 200, data

    return 400, {
        "error": "knowledge_base_upstream_error",
        "message": str(msg) if msg is not None else "",
        "code": code,
    }


def post_json(route_key: str, body: dict[str, Any]) -> tuple[int, Any]:
    url = _downstream_url(route_key)
    try:
        with _client() as client:
            resp = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise KnowledgeBaseUpstreamError(str(exc)) from exc
    return parse_upstream_response(resp)


def post_multipart(
    route_key: str,
    *,
    files: list[tuple[str, tuple[str, bytes, str | None]]],
    data: dict[str, str],
) -> tuple[int, Any]:
    url = _downstream_url(route_key)
    try:
        with _client(timeout=_UPLOAD_TIMEOUT) as client:
            resp = client.post(url, data=data, files=files)
    except httpx.HTTPError as exc:
        raise KnowledgeBaseUpstreamError(str(exc)) from exc
    return parse_upstream_response(resp)


def _location_create() -> str:
    return DEFAULT_LOCATION


def _location_edit() -> int:
    return int(DEFAULT_LOCATION)


def build_list_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": body["account"],
        "uuid": body["uuid"],
        "isPersonal": body["isPersonal"],
    }


def build_joined_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": body["account"],
        "uuid": body["uuid"],
    }


def build_create_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account": body["account"],
        "uuid": body["uuid"],
        "showName": body["showName"],
        "kbIntro": body.get("kbIntro", ""),
        "vsType": VS_TYPE,
        "embedModel": EMBED_MODEL,
        "iconType": ICON_TYPE,
        "isPersonal": body["isPersonal"],
        "location": body.get("location", _location_create()),
    }
    if "iconType" in body and body["iconType"] is not None:
        payload["iconType"] = body["iconType"]
    return payload


def build_info_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {"kbName": body["kbName"]}


def build_edit_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kbName": body["kbName"],
        "showName": body["showName"],
        "kbIntro": body.get("kbIntro", ""),
        "iconType": body.get("iconType", ICON_TYPE),
        "location": body.get("location", _location_edit()),
    }
    return payload


def build_delete_kb_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": body["account"],
        "kbName": body["kbName"],
    }


def build_available_payload(body: dict[str, Any], default_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    return {
        "account": body["account"],
        "uuid": body["uuid"],
        "showName": body.get("showName", ""),
        "kbIntro": body.get("kbIntro", ""),
        "page": body["page"],
        "size": body.get("size", default_size),
    }


def build_apply_join_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": body["account"],
        "uuid": body["uuid"],
        "kbName": body["kbName"],
    }


def build_members_payload(body: dict[str, Any], default_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    return {
        "uuid": body["uuid"],
        "kbName": body["kbName"],
        "username": body.get("username", ""),
        "account": body.get("account", ""),
        "page": body["page"],
        "size": body.get("size", default_size),
    }


def build_documents_payload(body: dict[str, Any], default_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    return {
        "kbName": body["kbName"],
        "fileLevel": body.get("fileLevel", ""),
        "fileName": body.get("fileName", ""),
        "fileNumber": body.get("fileNumber", ""),
        "fileClass": body.get("fileClass", ""),
        "fileOrg": body.get("fileOrg", ""),
        "fileExt": body.get("fileExt", ""),
        "status": body.get("status", []),
        "page": body["page"],
        "size": body.get("size", default_size),
        "sortName": body.get("sortName", ""),
        "sortOrder": body.get("sortOrder", ""),
    }


def build_update_docs_payload(body: dict[str, Any]) -> dict[str, Any]:
    file_properties = body.get("fileProperties")
    if not isinstance(file_properties, list):
        file_properties = []
    return {
        "kbName": body["kbName"],
        "fileNames": body["fileNames"],
        "fileProperties": file_properties,
    }


def build_delete_docs_payload(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "kbName": body["kbName"],
        "fileNames": body["fileNames"],
        "deleteContent": body.get("deleteContent", DELETE_CONTENT),
        "notRefreshVsCache": body.get("notRefreshVsCache", NOT_REFRESH_VS_CACHE),
    }


def parse_file_properties_json(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("fileProperties must be a JSON array")
    return [dict(item) for item in parsed if isinstance(item, dict)]


def build_upload_form_data(
    *,
    kb_name: str,
    file_properties_raw: str,
    chunk_size: str | None = None,
    chunk_overlap: str | None = None,
) -> dict[str, str]:
    return {
        "fileProperties": file_properties_raw,
        "kbName": kb_name,
        "chunkSize": chunk_size or DEFAULT_CHUNK_SIZE,
        "chunkOverlap": chunk_overlap or DEFAULT_CHUNK_OVERLAP,
    }
