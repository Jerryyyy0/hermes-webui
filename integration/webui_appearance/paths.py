"""Resolve paths under the base ``~/.hermes/webui-appearance/`` directory."""

from __future__ import annotations

from pathlib import Path

from api.workspace import safe_resolve_ws

_APPEARANCE_DIR = "webui-appearance"
_CONFIG_NAME = "webui-appearance.json"


def hermes_home() -> Path:
    """Return the default/root Hermes home, not the runtime profile pointer.

    ``os.environ['HERMES_HOME']`` is mutated per profile during streaming,
    assistant-bubble generation, cron runs, etc. Appearance assets are
    process-wide and live under the base home (``default`` profile / ``~/.hermes``).
    """
    from api.profiles import _DEFAULT_HERMES_HOME

    return Path(_DEFAULT_HERMES_HOME).resolve()


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
