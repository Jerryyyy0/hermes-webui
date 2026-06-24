"""Delete files under the integration workspace root."""

from __future__ import annotations

from pathlib import Path

from api.workspace import is_workspace_cruft_basename, unlink_anchored

from integration.workspace._root import integration_workspace_root, resolve_integration_rel
from integration.workspace.file_index_cache import invalidate_workspace_file_index


def delete_integration_workspace_files(paths: list[str]) -> dict:
    """Delete workspace files by relative path.

    Returns dict with workspace, deleted, and failed keys.
    """
    ws_root = integration_workspace_root()
    deleted: list[str] = []
    failed: list[dict[str, str]] = []

    for raw in paths:
        rel = str(raw or "").strip()
        if not rel:
            failed.append({"path": str(raw or ""), "error": "path is required"})
            continue
        try:
            target = resolve_integration_rel(rel)
        except ValueError as e:
            failed.append({"path": rel, "error": str(e)})
            continue

        if not target.exists():
            failed.append({"path": rel, "error": "not found"})
            continue
        if target.is_dir():
            failed.append({"path": rel, "error": "not a file"})
            continue
        if is_workspace_cruft_basename(target.name):
            failed.append({"path": rel, "error": "not found"})
            continue

        try:
            unlink_anchored(ws_root, target)
            deleted.append(rel)
        except (FileNotFoundError, PermissionError, OSError) as e:
            failed.append({"path": rel, "error": str(e)})

    if deleted:
        invalidate_workspace_file_index(ws_root)

    return {
        "workspace": str(ws_root),
        "deleted": deleted,
        "failed": failed,
    }
