"""HTTP handlers for session-less workspace files under DEFAULT_WORKSPACE."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import _sanitize_error, bad, j
from api.config import MAX_UPLOAD_BYTES
from api.upload import parse_multipart
from integration.session_manifest.store import (
    get_artifact_paths_for_profile,
    get_artifact_profile_index,
)
from api.workspace import (
    WORKSPACE_FILE_SORT_FIELDS,
    WORKSPACE_FILE_SORT_ORDERS,
    collect_workspace_file_entries_for_paths,
    is_workspace_cruft_basename,
    normalize_workspace_file_ext,
    paginate_workspace_file_entries,
)

from integration.config import integration_enabled
from integration.workspace._root import integration_workspace_root, resolve_integration_rel
from integration.workspace.file_index_cache import get_workspace_file_entries, invalidate_workspace_file_index
from integration.workspace.save import overwrite_workspace_file

_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 500
_MAX_PAGE_SIZE = 5000
_DEFAULT_SORT = "path"
_DEFAULT_ORDER = "desc"
_DEBUG_TIMING = os.environ.get("HERMES_DEBUG_TIMING") == "1"


def _parse_positive_int(raw: str | None, default: int) -> int | None:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return value


def _workspace_str() -> str:
    return str(integration_workspace_root())


def _query_str(qs: dict, key: str, default: str = "") -> str:
    raw = qs.get(key, [default])[0]
    return raw if raw is not None else default


def _truthy_query(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes"}


def _handle_files_list(handler, parsed) -> bool:
    qs = parse_qs(parsed.query)
    rel = _query_str(qs, "path", ".") or "."
    page = _parse_positive_int(qs.get("page", [None])[0], _DEFAULT_PAGE)
    page_size = _parse_positive_int(qs.get("page_size", [None])[0], _DEFAULT_PAGE_SIZE)
    if page is None:
        bad(handler, "page must be a positive integer", status=400)
        return True
    if page_size is None:
        bad(handler, "page_size must be a positive integer", status=400)
        return True
    if page_size > _MAX_PAGE_SIZE:
        page_size = _MAX_PAGE_SIZE

    q_raw = _query_str(qs, "q", "")
    q = q_raw.strip() or None

    type_raw = _query_str(qs, "type", "").strip()
    type_ext = normalize_workspace_file_ext(type_raw) if type_raw else None
    if type_raw and type_ext is None:
        bad(handler, "type must be a file extension such as .md or .py", status=400)
        return True

    sort = _query_str(qs, "sort", _DEFAULT_SORT).strip() or _DEFAULT_SORT
    if sort not in WORKSPACE_FILE_SORT_FIELDS:
        bad(
            handler,
            f"sort must be one of: {', '.join(sorted(WORKSPACE_FILE_SORT_FIELDS))}",
            status=400,
        )
        return True

    order = _query_str(qs, "order", _DEFAULT_ORDER).strip() or _DEFAULT_ORDER
    if order not in WORKSPACE_FILE_SORT_ORDERS:
        bad(handler, "order must be asc or desc", status=400)
        return True

    force_refresh = _truthy_query(_query_str(qs, "refresh", ""))
    root = integration_workspace_root()
    if force_refresh:
        invalidate_workspace_file_index(root, rel)

    profile_filter = _query_str(qs, "profile", "").strip() or None
    allowed_paths = None
    artifact_index: dict[str, str] = {}

    t0 = time.perf_counter()
    if profile_filter:
        allowed_paths = get_artifact_paths_for_profile(profile_filter, workspace_root=root)
    else:
        artifact_index = get_artifact_profile_index(workspace_root=root)
    artifact_ms = (time.perf_counter() - t0) * 1000.0

    try:
        t1 = time.perf_counter()
        if profile_filter and allowed_paths is not None:
            entries = collect_workspace_file_entries_for_paths(root, allowed_paths)
        else:
            entries = get_workspace_file_entries(root, rel, force_refresh=force_refresh)
        collect_ms = (time.perf_counter() - t1) * 1000.0

        t2 = time.perf_counter()
        result = paginate_workspace_file_entries(
            entries,
            page=page,
            page_size=page_size,
            q=q,
            type_ext=type_ext,
            sort=sort,
            order=order,
        )
        sort_ms = (time.perf_counter() - t2) * 1000.0
    except FileNotFoundError as e:
        bad(handler, _sanitize_error(e), status=404)
        return True
    except ValueError as e:
        bad(handler, _sanitize_error(e), status=400)
        return True

    files = result.get("files") or []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if profile_filter:
            entry["profile"] = profile_filter
            continue
        prof = artifact_index.get(path)
        if prof:
            entry["profile"] = prof

    extra_headers = None
    if _DEBUG_TIMING:
        extra_headers = {
            "X-Hermes-Timing-Artifact-Ms": f"{artifact_ms:.2f}",
            "X-Hermes-Timing-Collect-Ms": f"{collect_ms:.2f}",
            "X-Hermes-Timing-Sort-Ms": f"{sort_ms:.2f}",
        }

    j(
        handler,
        {
            "workspace": _workspace_str(),
            "path": rel,
            "page": page,
            "page_size": page_size,
            "q": q or "",
            "type": type_ext or "",
            "sort": sort,
            "order": order,
            "profile": profile_filter or "",
            "total": result.get("total", 0),
            "has_more": bool(result.get("has_more")),
            "files": files,
        },
        extra_headers=extra_headers,
    )
    return True


def _handle_file_stream(handler, parsed) -> bool:
    qs = parse_qs(parsed.query)
    rel = qs.get("path", [""])[0]
    if not rel:
        bad(handler, "path is required", status=400)
        return True

    if Path(rel).expanduser().is_absolute():
        from integration.session_manifest.external_references.preview import serve_registered_external_artifact

        if not serve_registered_external_artifact(handler, rel):
            j(handler, {"error": "文件不存在或无权预览"}, status=404)
        return True

    try:
        target = resolve_integration_rel(rel)
    except ValueError as e:
        bad(handler, _sanitize_error(e), status=404)
        return True

    if not target.exists() or not target.is_file():
        j(handler, {"error": "not found"}, status=404)
        return True
    if is_workspace_cruft_basename(target.name):
        j(handler, {"error": "not found"}, status=404)
        return True

    from api.routes import MIME_MAP, _serve_file_bytes

    mime = MIME_MAP.get(target.suffix.lower(), "application/octet-stream")
    _serve_file_bytes(handler, target, mime, None, "no-store")
    return True


def _parse_paths_body(body: dict | None) -> list[str] | None:
    if not isinstance(body, dict):
        return None
    raw = body.get("paths")
    if not isinstance(raw, list) or not raw:
        return None
    paths: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        paths.append(item)
    return paths


def _handle_file_delete(handler, body) -> bool:
    paths = _parse_paths_body(body)
    if paths is None:
        bad(handler, "paths must be a non-empty array of strings", status=400)
        return True

    from integration.workspace.delete import delete_integration_workspace_files

    result = delete_integration_workspace_files(paths)
    deleted = result.get("deleted") or []
    payload = {
        "ok": bool(deleted),
        "workspace": result.get("workspace", _workspace_str()),
        "deleted": deleted,
        "failed": result.get("failed") or [],
    }
    status = 200 if deleted else 404
    j(handler, payload, status=status)
    return True


def _handle_file_overwrite(handler) -> bool:
    content_type = handler.headers.get("Content-Type", "")
    content_length = handler.headers.get("Content-Length", 0)
    try:
        fields, files = parse_multipart(
            handler.rfile,
            content_type,
            content_length,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except ValueError as exc:
        message = "文件过大" if "too large" in str(exc).lower() else "请求格式无效"
        bad(handler, message, status=413 if message == "文件过大" else 400)
        return True

    path = fields.get("path")
    if not isinstance(path, str) or not path.strip():
        bad(handler, "path 为必填字段", status=400)
        return True
    if "file" not in files:
        bad(handler, "file 为必填文件字段", status=400)
        return True

    _filename, data = files["file"]
    try:
        result = overwrite_workspace_file(path, data)
    except FileNotFoundError:
        bad(handler, "文件不存在", status=404)
        return True
    except IsADirectoryError:
        bad(handler, "不能保存目录", status=400)
        return True
    except ValueError as exc:
        bad(handler, str(exc), status=400)
        return True
    except (PermissionError, OSError):
        bad(handler, "文件写入失败", status=500, exc_info=True)
        return True

    j(handler, {"ok": True, **result})
    return True


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False

    if parsed.path == "/api/integration/workspace/files":
        return _handle_files_list(handler, parsed)
    if parsed.path == "/api/integration/workspace/file":
        return _handle_file_stream(handler, parsed)

    return False


def try_handle_post(handler, parsed, body) -> bool:
    if not integration_enabled():
        return False

    if parsed.path == "/api/integration/workspace/file/delete":
        return _handle_file_delete(handler, body)

    return False


def try_handle_post_early(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == "/api/integration/workspace/file/overwrite":
        return _handle_file_overwrite(handler)
    return False
