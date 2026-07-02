"""HTTP handlers for knowledge base BFF proxy (/api/integration/knowledge_base/*)."""

from __future__ import annotations

import json
from typing import Any

from api.helpers import MAX_BODY_BYTES, _sanitize_error, bad, j, read_body

from integration.config import knowledge_base_enabled
from integration.knowledge_base.constants import (
    BINARY_PASSTHROUGH_ROUTES,
    DEFAULT_PAGE_SIZE,
    PASSTHROUGH_ROUTES,
)
from integration.knowledge_base import client
from integration.knowledge_base.constants import WEBUI_ROUTE_PREFIX

_ROUTE_BUILDERS: dict[str, str] = {
    "list": "build_list_payload",
    "joined": "build_joined_payload",
    "create": "build_create_payload",
    "info": "build_info_payload",
    "edit": "build_edit_payload",
    "delete": "build_delete_kb_payload",
    "available": "build_available_payload",
    "apply_join": "build_apply_join_payload",
    "members": "build_members_payload",
    "documents": "build_documents_payload",
    "update_docs": "build_update_docs_payload",
    "delete_docs": "build_delete_docs_payload",
    "show_pdf": "build_show_pdf_payload",
    "search_docs": "build_search_docs_payload",
    "search_docs_xcore": "build_search_docs_xcore_payload",
}

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "list": ("account", "uuid", "isPersonal"),
    "joined": ("account", "uuid"),
    "create": ("account", "uuid", "showName", "isPersonal"),
    "info": ("kbName",),
    "edit": ("kbName", "showName"),
    "delete": ("account", "kbName"),
    "available": ("account", "uuid", "page", "size"),
    "apply_join": ("account", "uuid", "kbName"),
    "members": ("uuid", "kbName", "page", "size"),
    "documents": ("kbName", "page", "size"),
    "update_docs": ("kbName", "fileNames", "fileProperties"),
    "delete_docs": ("kbName", "fileNames"),
    "show_pdf": ("kbName", "fileName"),
    "search_docs": ("query", "kbName"),
    "search_docs_xcore": ("query", "kbNames"),
    "upload_artifacts": ("uuid", "kbName", "fileProperties", "paths"),
}

_UPLOAD_ARTIFACTS_ERROR_CN: dict[str, str] = {
    "missing_uuid": "缺少用户 UUID",
    "missing_kbName": "缺少知识库名称",
    "missing_fileProperties": "缺少文件属性",
    "missing_paths": "缺少路径",
    "count_mismatch": "文件属性与路径数量不一致",
}


def _upload_artifacts_error_cn(code: str) -> str:
    return _UPLOAD_ARTIFACTS_ERROR_CN.get(code, code)


def _route_key(parsed) -> str | None:
    path = parsed.path
    if not path.startswith(WEBUI_ROUTE_PREFIX):
        return None
    key = path[len(WEBUI_ROUTE_PREFIX) :]
    if key in _ROUTE_BUILDERS or key == "upload_docs" or key == "upload_artifacts" or key in PASSTHROUGH_ROUTES:
        return key
    return None


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
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


def _respond_bad(handler, msg: str, status: int = 400) -> bool:
    bad(handler, msg, status=status)
    return True


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
    if route_key == "update_docs":
        file_names = body.get("fileNames")
        if not isinstance(file_names, list) or not file_names:
            return "missing_fileNames"
        file_properties = body.get("fileProperties")
        if not isinstance(file_properties, list) or not file_properties:
            return "missing_fileProperties"
    if route_key == "delete_docs":
        file_names = body.get("fileNames")
        if not isinstance(file_names, list) or not file_names:
            return "missing_fileNames"
    if route_key == "search_docs_xcore":
        kb_names = body.get("kbNames")
        if not isinstance(kb_names, list) or not kb_names:
            return "missing_kbNames"
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


def _build_upstream_payload(route_key: str, body: dict[str, Any]) -> dict[str, Any]:
    builder_name = _ROUTE_BUILDERS[route_key]
    builder = getattr(client, builder_name)
    if route_key in ("available", "members", "documents"):
        return builder(body, DEFAULT_PAGE_SIZE)
    return builder(body)


def _handle_upstream(handler, route_key: str, upstream_body: dict[str, Any]) -> bool:
    try:
        status, payload = client.post_json(route_key, upstream_body)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            {
                "error": "knowledge_base_upstream_failed",
                "message": _sanitize_error(exc),
            },
            status=502,
        )
    return _respond(handler, payload, status=status)


def _handle_binary_passthrough(handler, route_key: str, upstream_body: dict[str, Any]) -> bool:
    try:
        result = client.post_binary_or_json(route_key, upstream_body)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            {
                "error": "knowledge_base_upstream_failed",
                "message": _sanitize_error(exc),
            },
            status=502,
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


def _handle_show_pdf(handler, upstream_body: dict[str, Any]) -> bool:
    return _handle_binary_passthrough(handler, "show_pdf", upstream_body)


def try_handle_post_early(handler, parsed) -> bool:
    if not knowledge_base_enabled():
        return False
    route_key = _route_key(parsed)
    if route_key != "upload_docs":
        return False
    return _handle_upload_docs(handler)


def _upload_multipart_error(exc: ValueError) -> str:
    message = str(exc)
    known = {
        "No boundary in Content-Type": "Content-Type 缺少 boundary",
        "Invalid filename": "文件名无效",
    }
    return known.get(message, "请求格式无效")


def _handle_upload_docs(handler) -> bool:
    content_type = str(handler.headers.get("Content-Type", "") or "")
    content_length = int(handler.headers.get("Content-Length", 0) or 0)
    max_mb = MAX_BODY_BYTES // 1024 // 1024
    if content_length > MAX_BODY_BYTES:
        return _respond_bad(handler, f"请求体过大（最大 {max_mb}MB）", 413)

    if "multipart/form-data" not in content_type:
        return _respond_bad(handler, "Content-Type 须为 multipart/form-data", 400)

    from api.upload import parse_multipart

    try:
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
    except ValueError as exc:
        return _respond_bad(handler, _upload_multipart_error(exc), 400)

    kb_name = str(fields.get("kbName", "") or "").strip()
    uuid = str(fields.get("uuid", "") or "").strip()
    if not kb_name:
        return _respond_bad(handler, "missing_kbName", 400)
    if not uuid:
        return _respond_bad(handler, "missing_uuid", 400)

    upload_files = [(name, data) for name, data in files.items() if name == "files"]
    if not upload_files:
        for key, value in files.items():
            if key.startswith("files"):
                upload_files.append((key, value))
    if not upload_files:
        return _respond_bad(handler, "missing_files", 400)

    file_properties_raw = str(fields.get("fileProperties", "") or "")
    try:
        client.parse_file_properties_json(file_properties_raw)
    except (ValueError, TypeError) as exc:
        return _respond_bad(handler, _sanitize_error(exc), 400)

    httpx_files: list[tuple[str, tuple[str, bytes, str | None]]] = []
    for _, (filename, file_bytes) in upload_files:
        if not filename:
            return _respond_bad(handler, "missing_filename", 400)
        httpx_files.append(
            ("files", (filename, file_bytes, "application/octet-stream")),
        )

    form_data = client.build_upload_form_data(
        kb_name=kb_name,
        file_properties_raw=file_properties_raw,
        chunk_size=str(fields.get("chunkSize", "") or "").strip() or None,
        chunk_overlap=str(fields.get("chunkOverlap", "") or "").strip() or None,
    )

    try:
        status, payload = client.post_multipart("upload_docs", files=httpx_files, data=form_data)
    except client.KnowledgeBaseUpstreamError as exc:
        return _respond(
            handler,
            {
                "error": "knowledge_base_upstream_failed",
                "message": _sanitize_error(exc),
            },
            status=502,
        )

    return _respond(handler, payload, status=status)


def try_handle_post(handler, parsed, body) -> bool:
    if not knowledge_base_enabled():
        return False
    route_key = _route_key(parsed)
    if not route_key or route_key == "upload_docs":
        return False

    payload_body = _body_dict(body)

    if route_key in PASSTHROUGH_ROUTES:
        if route_key in BINARY_PASSTHROUGH_ROUTES:
            return _handle_binary_passthrough(handler, route_key, payload_body)
        return _handle_upstream(handler, route_key, payload_body)

    missing = _validate_required(payload_body, route_key)
    if missing:
        if route_key == "upload_artifacts":
            missing = _upload_artifacts_error_cn(missing)
        return _respond_bad(handler, missing, 400)

    if route_key == "upload_artifacts":
        return _handle_upload_artifacts(handler, payload_body)

    upstream_body = _build_upstream_payload(route_key, payload_body)
    if route_key == "show_pdf":
        return _handle_show_pdf(handler, upstream_body)
    return _handle_upstream(handler, route_key, upstream_body)


def _handle_upload_artifacts(handler, body: dict[str, Any]) -> bool:
    from api.workspace import resolve_trusted_workspace, safe_resolve_ws
    from integration.knowledge_base.constants import (
        MAX_ARTIFACT_FILE_BYTES,
        MAX_ARTIFACT_TOTAL_BYTES,
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
    total = 0
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
        total += size
        if total > MAX_ARTIFACT_TOTAL_BYTES:
            return _respond_bad(handler, "请求体过大", 413)
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
            {"error": "知识库服务不可用", "message": _sanitize_error(exc)},
            status=502,
        )
    return _respond(handler, payload, status=status)
