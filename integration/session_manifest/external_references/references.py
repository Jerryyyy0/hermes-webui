"""Provenance checks for external Artifact records."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

from .policy import close_fd_quietly, normalize_external_path, open_external_regular_file

_MUTATION_OR_EXECUTION_TOOLS = frozenset({
    "write_file", "create_file", "edit_file", "patch", "apply_patch",
    "mcp_filesystem_write_file", "mcp_filesystem_edit_file", "terminal",
})
_ASSISTANT_PROSE_SOURCE = "assistant_prose"
_MEDIA_SOURCE = "media"


def is_external_artifact_reference(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    source_tool = str(row.get("source_tool") or "").strip()
    if source_tool == _MEDIA_SOURCE:
        return False
    if source_tool not in _MUTATION_OR_EXECUTION_TOOLS and source_tool != _ASSISTANT_PROSE_SOURCE:
        return False
    return normalize_external_path(str(row.get("path") or "")) is not None


def is_registered_external_preview_reference(row: dict[str, Any] | None) -> bool:
    """Return whether a persisted row may use the absolute-path preview branch.

    ``media`` keeps its separate Artifact provenance and derivation rules, but a
    persisted absolute media Artifact must still be able to use the existing
    read-only workspace preview URL.  This grants no path-only access: callers
    use it only after an exact lookup in ``session_manifest_records``.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("record_kind") or "").strip() != "artifact":
        return False
    if str(row.get("preview") or "").strip() != "file":
        return False
    source_tool = str(row.get("source_tool") or "").strip()
    if source_tool == _MEDIA_SOURCE:
        return normalize_external_path(str(row.get("path") or "")) is not None
    return is_external_artifact_reference(row)


def external_artifact_path_is_safe(row: dict[str, Any] | None) -> bool:
    if not is_external_artifact_reference(row):
        return False
    opened = open_external_regular_file(str(row.get("path") or ""))
    if opened is None:
        return False
    fd, _path, _stat = opened
    close_fd_quietly(fd)
    return True


def registered_external_artifact(path: str | Path | None) -> dict[str, Any] | None:
    """Return the matching persisted external Artifact without creating a DB."""
    normalized = normalize_external_path(path)
    if normalized is None:
        return None
    db_path = (STATE_DIR / "session_manifest.db").expanduser()
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT path, source_tool, preview, record_kind
                FROM session_manifest_records
                WHERE record_kind = 'artifact' AND path = ?
                ORDER BY updated_at DESC
                """,
                (normalized.as_posix(),),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return None
    for raw in rows:
        row = dict(raw)
        if is_registered_external_preview_reference(row):
            return row
    return None
