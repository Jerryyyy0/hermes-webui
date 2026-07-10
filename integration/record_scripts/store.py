"""Filesystem storage for record scripts and their one-to-one CSV attachment."""

from __future__ import annotations

import re
from pathlib import Path

from api.config import STATE_DIR

_RECORD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class RecordScriptError(ValueError):
    """Raised for user-correctable record script storage errors."""


class RecordScriptNotFound(FileNotFoundError):
    """Raised when a record script resource does not exist."""


def record_scripts_root() -> Path:
    return (STATE_DIR / "attachments" / "record_scripts").resolve()


def validate_relate_name(relate_name: str) -> str:
    value = str(relate_name or "").strip()
    if not value:
        raise RecordScriptError("缺少 relate_name")
    if not _RECORD_NAME_RE.fullmatch(value):
        raise RecordScriptError("relate_name 格式无效")
    return value


def record_dir(relate_name: str) -> Path:
    name = validate_relate_name(relate_name)
    root = record_scripts_root()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise RecordScriptError("relate_name 路径无效")
    return target


def script_path(relate_name: str) -> Path:
    return record_dir(relate_name) / "script.json"


def csv_path(relate_name: str) -> Path:
    return record_dir(relate_name) / "data.csv"


def create_script(relate_name: str, script_json: str) -> dict:
    name = validate_relate_name(relate_name)
    content = _script_content(script_json)
    path = script_path(name)
    if path.exists():
        raise RecordScriptError("录制脚本已存在")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return _entry(name, path)


def list_scripts() -> list[dict]:
    root = record_scripts_root()
    if not root.exists():
        return []
    rows: list[dict] = []
    for path in sorted(root.glob("*/script.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            name = path.parent.name
            validate_relate_name(name)
            rows.append(_entry(name, path))
        except (OSError, RecordScriptError, UnicodeDecodeError):
            continue
    return rows


def update_script(relate_name: str, script_json: str) -> dict:
    name = validate_relate_name(relate_name)
    content = _script_content(script_json)
    path = script_path(name)
    if not path.exists():
        raise RecordScriptNotFound("录制脚本不存在")
    path.write_text(content, encoding="utf-8")
    return _entry(name, path)


def delete_script(relate_name: str) -> dict:
    name = validate_relate_name(relate_name)
    path = script_path(name)
    if not path.exists():
        raise RecordScriptNotFound("录制脚本不存在")
    path.unlink()
    return {"ok": True, "relate_name": name}


def save_csv(relate_name: str, data: bytes) -> dict:
    name = validate_relate_name(relate_name)
    if not script_path(name).exists():
        raise RecordScriptNotFound("录制脚本不存在")
    path = csv_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "ok": True,
        "relate_name": name,
        "filename": path.name,
        "size": path.stat().st_size,
    }


def delete_csv(relate_name: str) -> dict:
    name = validate_relate_name(relate_name)
    path = csv_path(name)
    if not path.exists():
        raise RecordScriptNotFound("CSV 文件不存在")
    path.unlink()
    return {"ok": True, "relate_name": name}


def require_csv(relate_name: str) -> Path:
    name = validate_relate_name(relate_name)
    path = csv_path(name)
    if not path.exists() or not path.is_file():
        raise RecordScriptNotFound("CSV 文件不存在")
    return path


def _script_content(script_json: str) -> str:
    if not isinstance(script_json, str):
        raise RecordScriptError("script_json 必须是字符串")
    return script_json


def _entry(relate_name: str, path: Path) -> dict:
    stat = path.stat()
    return {
        "relate_name": relate_name,
        "script_json": path.read_text(encoding="utf-8"),
        "has_csv": csv_path(relate_name).exists(),
        "updated_at": stat.st_mtime,
        "size": stat.st_size,
    }
