"""HTTP handlers for knowledge base BFF proxy (/api/integration/knowledge_base/*)."""

from __future__ import annotations

import json
from typing import Any

from api.helpers import bad, j

from integration.config import knowledge_base_enabled
from integration.knowledge_base.constants import (
    BINARY_PASSTHROUGH_ROUTES,
    WEBUI_ROUTE_PREFIX,
    WEBUI_ROUTE_TO_ROUTE_KEY,
)
from integration.knowledge_base import client

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "upload_artifacts": ("uuid", "kbName", "fileProperties", "paths"),
}

_VALIDATION_ERROR_CN: dict[str, str] = {
    "missing_uuid": "缺少用户 UUID",
    "missing_kbName": "缺少知识库名称",
    "missing_fileProperties": "缺少文件属性",
    "missing_paths": "缺少路径",
    "count_mismatch": "文件属性与路径数量不一致",
}


def _validation_error_cn(code: str) -> str:
    return _VALIDATION_ERROR_CN.get(code, "请求参数无效")


def _route_key(parsed) -> str | None:
    path = parsed.path
    if not path.startswith(WEBUI_ROUTE_PREFIX):
        return None
    route = path[len(WEBUI_ROUTE_PREFIX) :]
    if route == "upload_artifacts":
        return route
    return WEBUI_ROUTE_TO_ROUTE_KEY.get(route)


def _respond(handler, payload, status: int = 200, *, exc_info=None) -> bool:
    j(handler, payload, status=status, exc_info=exc_info)
    return True


def _respond_binary(
    handler,
    content: bytes,
    *,
    content_type: str,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> bool:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(content)
    return True


def _respond_bad(handler, msg: str, status: int = 400, *, exc_info=None) -> bool:
    bad(handler, msg, status=status, exc_info=exc_info)
    return True


def _upstream_failure_payload() -> dict[str, str]:
    return {
        "error": "知识库服务异常",
        "message": client.UPSTREAM_FAILURE_MESSAGE,
    }


def _body_dict(body) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    return {}


def _missing_field(body: dict[str, Any], field: str) -> bool:
    if field not in body:
        return True
    value = body[field]
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _validate_required(body: dict[str, Any], route_key: str) -> str | None:
    for field in _REQUIRED_FIELDS.get(route_key, ()):
        if _missing_field(body, field):
            return f"missing_{field}"
    if route_key == "upload_artifacts":
        file_properties = body.get("fileProperties")
        if not isinstance(file_properties, list) or not file_properties:
            return "missing_fileProperties"
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths:
            return "missing_paths"
        if len(file_properties) != len(paths):
            return "count_mismatch"
    return None


def _handle_upstream(handler, route_key: str, upstream_body: dict[str, Any]) -> bool:
    try:
        status, payload = client.post_json(route_key, upstream_body)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            _upstream_failure_payload(),
            status=500,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    return _respond(handler, payload, status=status)


def _handle_binary_passthrough(handler, route_key: str, upstream_body: dict[str, Any]) -> bool:
    try:
        result = client.post_binary_or_json(route_key, upstream_body)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            _upstream_failure_payload(),
            status=500,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    if result.kind == "binary":
        return _respond_binary(
            handler,
            result.content,
            content_type=result.content_type,
            status=result.status,
            extra_headers=result.extra_headers,
        )
    return _respond(handler, result.payload, status=result.status)


def try_handle_post_early(handler, parsed) -> bool:
    if not knowledge_base_enabled():
        return False
    route_key = _route_key(parsed)
    if route_key != "upload_docs":
        return False
    return _handle_upload_docs(handler)


def _handle_upload_docs(handler) -> bool:
    content_type = str(handler.headers.get("Content-Type", "") or "")
    try:
        content_length = int(handler.headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return _respond_bad(handler, "Content-Length 无效", 400)
    if content_length < 0:
        return _respond_bad(handler, "Content-Length 无效", 400)

    body = handler.rfile.read(content_length)
    if len(body) != content_length:
        return _respond_bad(handler, "请求体不完整", 400)

    try:
        status, payload = client.post_raw_body(
            "upload_docs",
            body=body,
            content_type=content_type,
        )
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            _upstream_failure_payload(),
            status=500,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    return _respond(handler, payload, status=status)


def try_handle_post(handler, parsed, body) -> bool:
    if not knowledge_base_enabled():
        return False
    route_key = _route_key(parsed)
    if not route_key or route_key == "upload_docs":
        return False

    payload_body = _body_dict(body)

    if route_key in BINARY_PASSTHROUGH_ROUTES:
        return _handle_binary_passthrough(handler, route_key, payload_body)

    if route_key == "upload_artifacts":
        missing = _validate_required(payload_body, route_key)
        if missing:
            return _respond_bad(handler, _validation_error_cn(missing), 400)
        return _handle_upload_artifacts(handler, payload_body)

    return _handle_upstream(handler, route_key, payload_body)


def _handle_upload_artifacts(handler, body: dict[str, Any]) -> bool:
    from api.workspace import resolve_trusted_workspace, safe_resolve_ws
    from integration.knowledge_base.constants import (
        MAX_ARTIFACT_FILE_BYTES,
        MAX_ARTIFACT_COUNT,
    )

    uuid = str(body.get("uuid", "") or "").strip()
    kb_name = str(body.get("kbName", "") or "").strip()
    file_properties = body.get("fileProperties") or []
    paths = body.get("paths") or []
    chunk_size = str(body.get("chunkSize", "") or "").strip() or None
    chunk_overlap = str(body.get("chunkOverlap", "") or "").strip() or None

    if len(paths) > MAX_ARTIFACT_COUNT:
        return _respond_bad(handler, "文件数量过多", 400)

    workspace = resolve_trusted_workspace(None)
    httpx_files: list[tuple[str, tuple[str, bytes, str | None]]] = []
    for rel in paths:
        rel = str(rel or "").strip()
        if not rel:
            return _respond_bad(handler, "缺少路径", 400)
        try:
            resolved = safe_resolve_ws(workspace, rel)
        except ValueError:
            return _respond_bad(handler, "路径越界", 400)
        if not resolved.is_file():
            return _respond_bad(handler, "文件不存在", 400)
        size = resolved.stat().st_size
        if size > MAX_ARTIFACT_FILE_BYTES:
            return _respond_bad(handler, "文件过大", 400)
        file_bytes = resolved.read_bytes()
        basename = resolved.name
        httpx_files.append(("files", (basename, file_bytes, "application/octet-stream")))

    file_properties_raw = json.dumps(file_properties, ensure_ascii=False)

    form_data = client.build_upload_form_data(
        kb_name=kb_name,
        file_properties_raw=file_properties_raw,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    try:
        status, payload = client.post_multipart("upload_docs", files=httpx_files, data=form_data)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            _upstream_failure_payload(),
            status=500,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    return _respond(handler, payload, status=status)
