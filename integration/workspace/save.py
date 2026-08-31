"""Save existing files in the integration workspace."""

from __future__ import annotations

import os
from pathlib import Path

from api.workspace import (
    is_workspace_cruft_basename,
    open_anchored_write_fd,
)

from integration.workspace._root import integration_workspace_root, resolve_integration_rel
from integration.workspace.file_index_cache import invalidate_workspace_file_index


def overwrite_workspace_file(path: str, data: bytes) -> dict:
    """Overwrite one existing regular workspace file and return write metadata."""
    rel = str(path or "").strip()
    if not rel:
        raise ValueError("path 为必填字段")
    if Path(rel).expanduser().is_absolute():
        raise ValueError("不允许保存绝对路径")

    root = integration_workspace_root()
    try:
        target = resolve_integration_rel(rel)
    except ValueError:
        raise ValueError("路径越界") from None

    if is_workspace_cruft_basename(target.name):
        raise FileNotFoundError("文件不存在")
    if not target.exists():
        raise FileNotFoundError("文件不存在")
    if target.is_dir():
        raise IsADirectoryError("不能保存目录")
    if not target.is_file():
        raise FileNotFoundError("文件不存在")

    fd = open_anchored_write_fd(root, target)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(data)
            handle.flush()
            stat_result = os.fstat(handle.fileno())
    finally:
        if fd is not None:
            os.close(fd)

    invalidate_workspace_file_index(root)
    return {"size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns}
