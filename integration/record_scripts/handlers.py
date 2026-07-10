"""HTTP handlers for record script JSON and CSV attachment APIs."""

from __future__ import annotations

from urllib.parse import parse_qs

from api.config import MAX_UPLOAD_BYTES
from api.helpers import _sanitize_error, bad, j
from api.upload import parse_multipart

from integration.config import integration_enabled
from integration.record_scripts import store

_SCRIPT_PREFIX = "/api/integration/record_scripts"
_CSV_PREFIX = "/api/integration/record_scripts_csv"


def _body_dict(body) -> dict:
    return body if isinstance(body, dict) else {}


def _body_str(body: dict, key: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) else ""


def _require_script_json(body: dict) -> str:
    if "script_json" not in body or not isinstance(body.get("script_json"), str):
        raise store.RecordScriptError("script_json 必须是字符串")
    return body["script_json"]


def _handle_script_save(handler, body) -> bool:
    payload = _body_dict(body)
    try:
        result = store.create_script(
            _body_str(payload, "relate_name"),
            _require_script_json(payload),
        )
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True
    j(handler, {"ok": True, **result})
    return True


def _handle_script_list(handler) -> bool:
    j(handler, {"ok": True, "scripts": store.list_scripts()})
    return True


def _handle_script_update(handler, body) -> bool:
    payload = _body_dict(body)
    try:
        result = store.update_script(
            _body_str(payload, "relate_name"),
            _require_script_json(payload),
        )
    except store.RecordScriptNotFound as exc:
        bad(handler, _sanitize_error(exc), status=404)
        return True
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True
    j(handler, {"ok": True, **result})
    return True


def _handle_script_delete(handler, body) -> bool:
    payload = _body_dict(body)
    try:
        result = store.delete_script(_body_str(payload, "relate_name"))
    except store.RecordScriptNotFound as exc:
        bad(handler, _sanitize_error(exc), status=404)
        return True
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True
    j(handler, result)
    return True


def _handle_csv_upload(handler) -> bool:
    content_type = handler.headers.get("Content-Type", "")
    content_length = handler.headers.get("Content-Length", 0) or 0
    try:
        if int(content_length) > MAX_UPLOAD_BYTES:
            j(handler, {"error": f"文件大小需控制在{MAX_UPLOAD_BYTES // 1024 // 1024}M以内"}, status=413)
            return True
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
    except ValueError as exc:
        status = 413 if "too large" in str(exc).lower() else 400
        bad(handler, _sanitize_error(exc), status=status)
        return True

    relate_name = fields.get("relate_name", "")
    if "file" not in files:
        bad(handler, "缺少上传文件", status=400)
        return True
    filename, file_bytes = files["file"]
    if not filename:
        bad(handler, "文件名无效或缺失", status=400)
        return True
    if not str(filename).lower().endswith(".csv"):
        bad(handler, "仅支持 CSV 文件", status=400)
        return True

    try:
        result = store.save_csv(relate_name, file_bytes)
    except store.RecordScriptNotFound as exc:
        bad(handler, _sanitize_error(exc), status=404)
        return True
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True
    j(handler, result)
    return True


def _handle_csv_download(handler, parsed) -> bool:
    qs = parse_qs(parsed.query)
    relate_name = qs.get("relate_name", [""])[0]
    try:
        path = store.require_csv(relate_name)
    except store.RecordScriptNotFound as exc:
        bad(handler, _sanitize_error(exc), status=404)
        return True
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True

    from api.routes import _serve_file_bytes

    _serve_file_bytes(handler, path, "text/csv; charset=utf-8", "attachment", "no-store")
    return True


def _handle_csv_delete(handler, body) -> bool:
    payload = _body_dict(body)
    try:
        result = store.delete_csv(_body_str(payload, "relate_name"))
    except store.RecordScriptNotFound as exc:
        bad(handler, _sanitize_error(exc), status=404)
        return True
    except store.RecordScriptError as exc:
        bad(handler, _sanitize_error(exc), status=400)
        return True
    j(handler, result)
    return True


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == f"{_SCRIPT_PREFIX}/list":
        return _handle_script_list(handler)
    if parsed.path == f"{_CSV_PREFIX}/download":
        return _handle_csv_download(handler, parsed)
    return False


def try_handle_post_early(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == f"{_CSV_PREFIX}/upload":
        return _handle_csv_upload(handler)
    return False


def try_handle_post(handler, parsed, body) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == f"{_SCRIPT_PREFIX}/save":
        return _handle_script_save(handler, body)
    if parsed.path == f"{_SCRIPT_PREFIX}/update":
        return _handle_script_update(handler, body)
    if parsed.path == f"{_SCRIPT_PREFIX}/delete":
        return _handle_script_delete(handler, body)
    if parsed.path == f"{_CSV_PREFIX}/delete":
        return _handle_csv_delete(handler, body)
    return False
