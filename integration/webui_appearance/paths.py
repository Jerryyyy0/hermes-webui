"""Resolve paths under ``{HERMES_HOME}/webui-appearance/``."""

from __future__ import annotations

import os
from pathlib import Path

from api.workspace import safe_resolve_ws

_APPEARANCE_DIR = "webui-appearance"
_CONFIG_NAME = "webui-appearance.json"


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()


def appearance_root() -> Path:
    return (hermes_home() / _APPEARANCE_DIR).resolve()


def config_path() -> Path:
    return appearance_root() / _CONFIG_NAME


def resolve_appearance_rel(rel: str) -> Path:
    """Resolve a relative path inside the appearance root.

    Raises ValueError on empty path, absolute path, or traversal.
    """
    raw = str(rel or "").strip()
    if not raw:
        raise ValueError("path 为必填参数")
    if Path(raw).is_absolute():
        raise ValueError("不允许使用绝对路径")
    try:
        return safe_resolve_ws(appearance_root(), raw)
    except ValueError as exc:
        raise ValueError("路径无效或不允许访问") from exc
