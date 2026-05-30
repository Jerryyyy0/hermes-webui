"""Per-profile MEMORY.md / USER.md / SOUL.md snapshot for GET /api/profiles enrich."""

from __future__ import annotations

import logging
from pathlib import Path

from api.helpers import _redact_text

_log = logging.getLogger(__name__)


def _empty_memory_snapshot() -> dict:
    return {
        "memory": "",
        "user": "",
        "soul": "",
        "memory_path": "",
        "user_path": "",
        "soul_path": "",
        "memory_mtime": None,
        "user_mtime": None,
        "soul_mtime": None,
    }


def load_memory_snapshot(profile_path: str) -> dict:
    """Read memory files under *profile_path* (HERMES_HOME), aligned with GET /api/memory."""
    raw = str(profile_path or "").strip()
    if not raw:
        return _empty_memory_snapshot()

    try:
        home = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as exc:
        _log.debug("memory_snapshot invalid profile path %r: %s", profile_path, exc)
        return _empty_memory_snapshot()

    if not home.is_dir():
        return _empty_memory_snapshot()

    mem_dir = home / "memories"
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    soul_file = home / "SOUL.md"

    try:
        memory = (
            mem_file.read_text(encoding="utf-8", errors="replace")
            if mem_file.is_file()
            else ""
        )
        user = (
            user_file.read_text(encoding="utf-8", errors="replace")
            if user_file.is_file()
            else ""
        )
        soul = (
            soul_file.read_text(encoding="utf-8", errors="replace")
            if soul_file.is_file()
            else ""
        )
    except OSError as exc:
        _log.debug("memory_snapshot read failed for %s: %s", home, exc)
        return _empty_memory_snapshot()

    return {
        "memory": _redact_text(memory),
        "user": _redact_text(user),
        "soul": _redact_text(soul),
    }
