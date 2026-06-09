"""HTTP handlers for session-less workspace files under DEFAULT_WORKSPACE."""

from __future__ import annotations

from urllib.parse import parse_qs

from api.helpers import _sanitize_error, bad, j
from api.workspace import (
    WORKSPACE_FILE_SORT_FIELDS,
    WORKSPACE_FILE_SORT_ORDERS,
    is_workspace_cruft_basename,
    normalize_workspace_file_ext,
    walk_workspace_files_page,
)

from integration.config import integration_enabled
from integration.workspace._root import integration_workspace_root, resolve_integration_rel

_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 500
_MAX_PAGE_SIZE = 5000
_DEFAULT_SORT = "path"
_DEFAULT_ORDER = "desc"


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

    root = integration_workspace_root()
    try:
        result = walk_workspace_files_page(
            root,
            rel,
            page=page,
            page_size=page_size,
            q=q,
            type_ext=type_ext,
            sort=sort,
            order=order,
        )
    except FileNotFoundError as e:
        bad(handler, _sanitize_error(e), status=404)
        return True
    except ValueError as e:
        bad(handler, _sanitize_error(e), status=400)
        return True

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
            "total": result.get("total", 0),
            "has_more": bool(result.get("has_more")),
            "files": result.get("files") or [],
        },
    )
    return True


def _handle_file_stream(handler, parsed) -> bool:
    qs = parse_qs(parsed.query)
    rel = qs.get("path", [""])[0]
    if not rel:
        bad(handler, "path is required", status=400)
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


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False

    if parsed.path == "/api/integration/workspace/files":
        return _handle_files_list(handler, parsed)
    if parsed.path == "/api/integration/workspace/file":
        return _handle_file_stream(handler, parsed)

    return False
