"""HTTP handlers for WebUI appearance config and asset preview."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from api.helpers import _sanitize_error, bad, j
from integration.config import integration_enabled
from integration.webui_appearance.paths import appearance_root, config_path, resolve_appearance_rel

_CONFIG_PATH = "/api/integration/webui_appearance"
_FILE_PATH = "/api/integration/webui_appearance/file"


def _handle_config(handler) -> bool:
    path = config_path()
    if not path.is_file():
        bad(handler, "外观配置文件不存在", status=404)
        return True
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError:
        bad(handler, "外观配置文件不是合法 JSON", status=400)
        return True
    except OSError as exc:
        bad(handler, _sanitize_error(exc) or "读取外观配置失败", status=500)
        return True
    if not isinstance(data, (dict, list)):
        bad(handler, "外观配置文件不是合法 JSON 对象或数组", status=400)
        return True
    j(handler, data)
    return True


def _handle_file(handler, parsed) -> bool:
    qs = parse_qs(parsed.query or "")
    rel = str((qs.get("path") or [""])[0] or "").strip()
    if not rel:
        bad(handler, "path 为必填参数", status=400)
        return True

    try:
        target = resolve_appearance_rel(rel)
    except ValueError as exc:
        bad(handler, _sanitize_error(exc) or "路径无效", status=400)
        return True

    if not target.exists() or not target.is_file():
        bad(handler, "文件不存在", status=404)
        return True

    from api.routes import MIME_MAP, _serve_file_bytes

    root = appearance_root()
    mime = MIME_MAP.get(target.suffix.lower(), "application/octet-stream")
    _serve_file_bytes(handler, target, mime, None, "no-store", anchor_root=root)
    return True


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == _CONFIG_PATH:
        return _handle_config(handler)
    if parsed.path == _FILE_PATH:
        return _handle_file(handler, parsed)
    return False
