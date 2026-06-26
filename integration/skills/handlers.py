"""HTTP handlers for SkillHub proxy routes (/api/skillhub/*)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import MAX_BODY_BYTES, bad, j, read_body

from integration.config import integration_enabled, skillhub_enabled
from integration.skills import listing, local_skills, no_self_improve, skillhub
from integration.skills.sort_utils import (
    SKILL_LIST_SORT_FIELDS,
    SKILL_LIST_SORT_ORDERS,
    normalize_order,
    normalize_sort,
)
from integration.skills.utils import stream_zip_to_handler


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, msg, status: int = 400) -> bool:
    bad(handler, msg, status=status)
    return True


def try_handle_get(handler, parsed) -> bool:
    path = parsed.path
    if path == "/api/skillhub/download":
        if not integration_enabled():
            return False
        return _get_skillhub_download(handler, parsed)
    if path == "/api/skillhub/skills/no_self_improve":
        if not integration_enabled():
            return False
        return _get_no_self_improve(handler)
    if not skillhub_enabled():
        return False
    if path == "/api/skillhub/skills":
        return _get_skillhub_skills(handler, parsed)
    if path == "/api/skillhub/categories":
        return _get_skillhub_categories(handler, parsed)
    if path == "/api/skillhub/detail":
        return _get_skillhub_detail(handler, parsed)
    if path == "/api/skillhub/content":
        return _get_skillhub_content(handler, parsed)
    if path == "/api/skillhub/structure":
        return _get_skillhub_structure(handler, parsed)
    if path == "/api/skillhub/file":
        return _get_skillhub_file(handler, parsed)
    return False


def try_handle_post_early(handler, parsed) -> bool:
    """POST routes that read the raw body (multipart). Run before read_body in routes."""
    path = parsed.path
    if path == "/api/skillhub/upload":
        return handle_skillhub_upload(handler)
    return False


def _parse_upload_overwrite(fields_or_body: dict) -> bool:
    raw = fields_or_body.get("overwrite")
    if raw is True:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes")
    return False


def _upload_multipart_error(exc: ValueError) -> str:
    message = str(exc)
    known = {
        "No boundary in Content-Type": "Content-Type 缺少 boundary",
        "Invalid filename": "文件名无效",
    }
    return known.get(message, "请求格式无效")


def handle_skillhub_upload(handler) -> bool:
    """POST /api/skillhub/upload — local custom skill only (no upstream)."""
    if not integration_enabled():
        return _respond_bad(handler, "集成未启用", 404)

    content_type = str(handler.headers.get("Content-Type", "") or "")
    content_length = int(handler.headers.get("Content-Length", 0) or 0)
    max_mb = MAX_BODY_BYTES // 1024 // 1024
    if content_length > MAX_BODY_BYTES:
        return _respond_bad(handler, f"请求体过大（最大 {max_mb}MB）", 413)

    if "multipart/form-data" in content_type:
        from api.upload import parse_multipart

        try:
            fields, files = parse_multipart(handler.rfile, content_type, content_length)
        except ValueError as exc:
            return _respond_bad(handler, _upload_multipart_error(exc), 400)

        if "file" not in files:
            return _respond_bad(handler, "缺少文件", 400)
        upload_name, file_bytes = files["file"]
        if not upload_name:
            return _respond_bad(handler, "缺少文件名", 400)
        suffix = Path(upload_name).suffix.lower()
        if suffix not in (".md", ".zip"):
            return _respond_bad(handler, "仅支持 .md 与 .zip 文件", 400)

        request_name = str(fields.get("name", "") or "").strip()
        category = str(fields.get("category", "") or "").strip()
        explicit_dir_name = str(fields.get("dir_name", "") or "").strip()
        overwrite = _parse_upload_overwrite(fields)

        if suffix == ".zip":
            result = local_skills.upload_custom_skill(
                request_name=request_name,
                category=category,
                zip_bytes=file_bytes,
                filename=upload_name,
                overwrite=overwrite,
                explicit_dir_name=explicit_dir_name,
            )
        else:
            text = file_bytes.decode("utf-8", errors="replace")
            result = local_skills.upload_custom_skill(
                request_name=request_name,
                category=category,
                content=text,
                filename=upload_name,
                overwrite=overwrite,
                explicit_dir_name=explicit_dir_name,
            )
    else:
        try:
            body = read_body(handler)
        except ValueError:
            return _respond_bad(handler, f"请求体过大（最大 {max_mb}MB）", 413)
        if not isinstance(body, dict):
            body = {}
        content = body.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            return _respond_bad(handler, "缺少 content", 400)
        result = local_skills.upload_custom_skill(
            request_name=str(body.get("name", "") or "").strip(),
            category=str(body.get("category", "") or "").strip(),
            content=str(content),
            filename=None,
            overwrite=_parse_upload_overwrite(body),
            explicit_dir_name=str(body.get("dir_name", "") or "").strip(),
        )

    return _upload_result(handler, result)


def _upload_result(handler, result: dict) -> bool:
    status = int(result.get("status") or 0)
    if result.get("error"):
        return _respond_bad(handler, str(result["error"]), status or 400)
    return _respond(handler, result)


def try_handle_post(handler, parsed, body: dict | None) -> bool:
    body = body if isinstance(body, dict) else {}
    path = parsed.path
    if path == "/api/skillhub/delete":
        if not integration_enabled():
            return False
        return _post_skillhub_delete(handler, body)
    if path == "/api/skillhub/edit":
        if not integration_enabled():
            return False
        return _post_skillhub_edit(handler, body)
    if path == "/api/skillhub/skills/no_self_improve/toggle":
        if not integration_enabled():
            return False
        return _post_no_self_improve_toggle(handler, body)
    if not skillhub_enabled():
        return False
    if path == "/api/skillhub/install":
        return _post_skillhub_install(handler, parsed, body)
    return False


def try_handle_put(handler, parsed, body: dict | None) -> bool:
    body = body if isinstance(body, dict) else {}
    if parsed.path == "/api/skillhub/skills/no_self_improve":
        if not integration_enabled():
            return False
        return _put_no_self_improve(handler, body)
    return False


def _qs(parsed) -> dict:
    return parse_qs(parsed.query or "")


def _optional_int(raw: str | None) -> int | None:
    if raw and str(raw).isdigit():
        return int(raw)
    return None


def _normalize_category(raw: str | None) -> str:
    value = str(raw or "").strip()
    if value.lower() in ("all", "*"):
        return ""
    return value


_PREVIEW_SCOPES = frozenset({"custom", "hub", "auto"})


def _skillhub_preview_scope(qs: dict) -> str:
    """Preview routes: default auto (local first, then SkillHub upstream)."""
    scope = str((qs.get("scope") or [""])[0]).strip().lower()
    if scope in _PREVIEW_SCOPES:
        return scope
    return "auto"


def _is_custom_scope(qs: dict) -> bool:
    return _skillhub_preview_scope(qs) == "custom"


def _preview_local_only(scope: str) -> bool:
    """custom/auto: read shared_skills_dir only (404 when missing)."""
    return scope in ("custom", "auto")


def _local_custom_result(handler, payload: dict) -> bool:
    if payload.get("error"):
        return _respond_bad(handler, str(payload["error"]), int(payload.get("status") or 404))
    return _respond(handler, payload)


def _get_skillhub_download(handler, parsed) -> bool:
    qs = _qs(parsed)
    name = (qs.get("name") or [""])[0]
    if not name:
        return _respond_bad(handler, "name required", 400)
    dir_name = (qs.get("dir_name") or [""])[0]
    result = local_skills.prepare_skill_download(name, dir_name)
    if result.get("error"):
        status = int(result.get("status") or 400)
        if status == 413:
            return _respond(handler, result, status=413)
        return _respond_bad(handler, str(result["error"]), status)
    stream_zip_to_handler(
        handler,
        str(result["zip_basename"]),
        result["files"],
        result.get("extra_zip_entries"),
    )
    return True


def _get_skillhub_skills(handler, parsed) -> bool:
    qs = _qs(parsed)
    scope = (qs.get("scope") or ["hub"])[0]
    category = _normalize_category((qs.get("category") or [""])[0])
    q = (qs.get("q") or [None])[0]
    page = _optional_int((qs.get("page") or [None])[0])
    page_size = _optional_int((qs.get("page_size") or [None])[0])
    all_records = (qs.get("all", [""])[0] or "").strip() == "1"
    sort_raw = str((qs.get("sort") or ["name"])[0] or "name").strip().lower()
    order_raw = str((qs.get("order") or ["asc"])[0] or "asc").strip().lower()
    if sort_raw not in SKILL_LIST_SORT_FIELDS:
        return _respond_bad(
            handler,
            f"sort must be one of: {', '.join(sorted(SKILL_LIST_SORT_FIELDS))}",
            400,
        )
    if order_raw not in SKILL_LIST_SORT_ORDERS:
        return _respond_bad(handler, "order must be asc or desc", 400)
    sort = normalize_sort(sort_raw)
    order = normalize_order(order_raw)
    try:
        payload = listing.list_skillhub_skills(
            scope=scope,
            category=category,
            q=q,
            page=page,
            page_size=page_size,
            sort=sort,
            order=order,
            all_records=all_records,
        )
        return _respond(handler, payload)
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _get_skillhub_categories(handler, parsed) -> bool:
    try:
        return _respond(handler, skillhub.fetch_categories())
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _get_skillhub_detail(handler, parsed) -> bool:
    qs = _qs(parsed)
    name = (qs.get("name") or [""])[0]
    if not name:
        return _respond_bad(handler, "name required", 400)
    try:
        return _respond(handler, skillhub.fetch_skill_detail(name))
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _get_skillhub_content(handler, parsed) -> bool:
    qs = _qs(parsed)
    name = (qs.get("name") or [""])[0]
    if not name:
        return _respond_bad(handler, "name required", 400)
    scope = _skillhub_preview_scope(qs)
    if _preview_local_only(scope):
        return _local_custom_result(handler, local_skills.get_custom_doc(name))
    if local_skills.has_local_skill(name):
        return _local_custom_result(handler, local_skills.get_custom_doc(name))
    try:
        return _respond(handler, skillhub.fetch_doc(name))
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _get_skillhub_structure(handler, parsed) -> bool:
    qs = _qs(parsed)
    name = (qs.get("name") or [""])[0]
    if not name:
        return _respond_bad(handler, "name required", 400)
    scope = _skillhub_preview_scope(qs)
    if _preview_local_only(scope):
        return _local_custom_result(handler, local_skills.get_custom_structure(name))
    if local_skills.has_local_skill(name):
        return _local_custom_result(handler, local_skills.get_custom_structure(name))
    try:
        return _respond(handler, skillhub.fetch_structure(name))
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _get_skillhub_file(handler, parsed) -> bool:
    qs = _qs(parsed)
    name = (qs.get("name") or [""])[0]
    file_path = (qs.get("path") or qs.get("file") or [""])[0]
    if not name:
        return _respond_bad(handler, "name required", 400)
    if not file_path:
        return _respond_bad(handler, "path required", 400)
    scope = _skillhub_preview_scope(qs)
    if _preview_local_only(scope):
        return _local_custom_result(handler, local_skills.get_custom_file(name, file_path))
    if local_skills.has_local_skill(name):
        return _local_custom_result(handler, local_skills.get_custom_file(name, file_path))
    try:
        return _respond(handler, skillhub.fetch_file(name, file_path))
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _post_skillhub_install(handler, parsed, body: dict) -> bool:
    name = str(body.get("name", "")).strip()
    if not name:
        return _respond_bad(handler, "name required", 400)
    display_name = str(body.get("display_name", "") or "").strip()
    category = str(body.get("category", "") or "").strip()
    if not category:
        try:
            detail = skillhub.fetch_skill_detail(name)
            category = str(detail.get("category") or "").strip()
        except Exception:
            category = ""
    try:
        result = skillhub.install_skill(name, display_name, category=category)
        if result.get("status") == 409:
            return _respond_bad(handler, result.get("error", "conflict"), 409)
        return _respond(handler, result)
    except RuntimeError as exc:
        return _respond_bad(handler, str(exc), 503)
    except Exception as exc:
        return _respond_bad(handler, str(exc), 502)


def _post_skillhub_edit(handler, body: dict) -> bool:
    name = str(body.get("name", "")).strip()
    if not name:
        return _respond_bad(handler, "name required", 400)
    content = body.get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        return _respond_bad(handler, "缺少 content", 400)
    dir_name = str(body.get("dir_name", "") or "").strip()
    result = local_skills.edit_custom_skill(
        name=name,
        content=str(content),
        dir_name=dir_name,
    )
    status = int(result.get("status") or 0)
    if result.get("error"):
        return _respond_bad(handler, result.get("error", "error"), status or 400)
    return _respond(handler, result)


def _post_skillhub_delete(handler, body: dict) -> bool:
    name = str(body.get("name", "")).strip()
    if not name:
        return _respond_bad(handler, "name required", 400)
    dir_name = str(body.get("dir_name", "") or "").strip()
    result = local_skills.delete_local_skill(name, dir_name)
    status = int(result.get("status") or 0)
    if status:
        return _respond_bad(handler, result.get("error", "error"), status)
    return _respond(handler, result)


def _get_no_self_improve(handler) -> bool:
    names = sorted(no_self_improve.get_no_self_improve_names())
    return _respond(
        handler,
        {"ok": True, "names": names, "count": len(names)},
    )


def _post_no_self_improve_toggle(handler, body: dict) -> bool:
    name = str(body.get("name", "")).strip()
    if not name:
        return _respond_bad(handler, "name required", 400)
    if "locked" not in body:
        return _respond_bad(handler, "locked required", 400)
    locked = bool(body["locked"])
    dir_name = str(body.get("dir_name", "") or "").strip()

    from integration.skills.paths import shared_skills_dir

    skills_dir = shared_skills_dir()
    skill_dir = local_skills._resolve_skill_dir(skills_dir, name, dir_name)
    if not skill_dir or not skill_dir.is_dir():
        return _respond_bad(handler, f"Skill '{name}' not found", 404)
    if (skill_dir / ".hub_installed").is_file():
        return _respond_bad(handler, "Hub skills are permanently locked", 403)

    if locked:
        no_self_improve.add_names([name])
    else:
        no_self_improve.remove_names([name])
    return _respond(handler, {"ok": True, "name": name, "locked": locked})


def _put_no_self_improve(handler, body: dict) -> bool:
    raw_names = body.get("names")
    if not isinstance(raw_names, list):
        return _respond_bad(handler, "names must be an array", 400)
    names = no_self_improve.normalize_names(raw_names)
    saved = no_self_improve.save_no_self_improve(names)
    return _respond(
        handler,
        {"ok": True, "names": saved, "count": len(saved)},
    )
