"""HTTP client for knowledge base downstream service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from integration.config import knowledge_base_url
from integration.knowledge_base.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_LOCATION,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    DELETE_CONTENT,
    EMBED_MODEL,
    ICON_TYPE,
    NOT_REFRESH_VS_CACHE,
    VS_TYPE,
    API_PREFIX,
    DOWNSTREAM_PATHS,
)

_TIMEOUT = 30.0
_SHOW_PDF_TIMEOUT = 180.0
_UPLOAD_TIMEOUT = 180.0
_LONG_BINARY_TIMEOUT_ROUTES = frozenset({"show_pdf", "download_doc"})
UPSTREAM_FAILURE_MESSAGE = "知识库服务异常，请稍后重试"


class KnowledgeBaseUpstreamError(Exception):
    """Downstream did not provide a usable response.

    The exception text is retained for server-side traceback logging. Handlers
    must use ``UPSTREAM_FAILURE_MESSAGE`` for the client-facing message.
    """


@dataclass(frozen=True)
class KnowledgeBaseShowPdfResult:
    kind: str  # "json" | "binary"
    status: int
    payload: Any = None
    content: bytes = b""
    content_type: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


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


def parse_upstream_response(resp: httpx.Response) -> tuple[int, Any]:
    """Return downstream HTTP status and JSON body unchanged."""
    try:
        payload = resp.json()
    except Exception as exc:
        raise KnowledgeBaseUpstreamError(
            f"invalid JSON from upstream (HTTP {resp.status_code})"
        ) from exc
    return resp.status_code, payload


def _content_type_base(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "").split(";")[0].strip().lower()


def _is_json_upstream_response(resp: httpx.Response) -> bool:
    content_type = _content_type_base(resp)
    if content_type in ("application/json", "text/json"):
        return True
    if content_type in ("application/pdf", "application/octet-stream"):
        return False
    content = resp.content
    if not content:
        return True
    return content.lstrip().startswith((b"{", b"["))


def post_binary_or_json(route_key: str, body: dict[str, Any]) -> KnowledgeBaseShowPdfResult:
    url = _downstream_url(route_key)
    timeout = _SHOW_PDF_TIMEOUT if route_key in _LONG_BINARY_TIMEOUT_ROUTES else _TIMEOUT
    try:
        with _client(timeout=timeout) as client:
            resp = client.post(url, json=body)
    except httpx.HTTPError as exc:
        raise KnowledgeBaseUpstreamError(str(exc)) from exc

    if _is_json_upstream_response(resp):
        status, payload = parse_upstream_response(resp)
        return KnowledgeBaseShowPdfResult(kind="json", status=status, payload=payload)

    if resp.status_code >= 400:
        raise KnowledgeBaseUpstreamError(
            f"non-JSON upstream error response (HTTP {resp.status_code})"
        )

    content_type = _content_type_base(resp) or "application/octet-stream"
    extra_headers: dict[str, str] = {}
    content_disposition = resp.headers.get("content-disposition")
    if content_disposition:
        extra_headers["Content-Disposition"] = content_disposition
    return KnowledgeBaseShowPdfResult(
        kind="binary",
        status=resp.status_code,
        content=resp.content,
        content_type=content_type,
        extra_headers=extra_headers,
    )


def post_show_pdf(body: dict[str, Any]) -> KnowledgeBaseShowPdfResult:
    return post_binary_or_json("show_pdf", body)


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


def post_raw_body(
    route_key: str,
    *,
    body: bytes,
    content_type: str,
) -> tuple[int, Any]:
    """Forward request body bytes to downstream unchanged."""
    url = _downstream_url(route_key)
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        with _client(timeout=_UPLOAD_TIMEOUT) as client:
            resp = client.post(url, content=body, headers=headers)
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


def build_search_docs_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": body["query"],
        "knowledge_base_name": body["kbName"],
        "top_k": body.get("topK", DEFAULT_TOP_K),
        "score_threshold": body.get("scoreThreshold", DEFAULT_SCORE_THRESHOLD),
    }
    return payload


def build_search_docs_xcore_payload(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": body["query"],
        "kbNames": body["kbNames"],
        "top_k": body.get("topK", DEFAULT_TOP_K),
        "score_threshold": body.get("scoreThreshold", DEFAULT_SCORE_THRESHOLD),
    }
    return payload


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
