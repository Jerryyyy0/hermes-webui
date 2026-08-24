"""Serve a registered external Artifact through the existing workspace URL."""

from __future__ import annotations

from .policy import close_fd_quietly, open_external_regular_file
from .references import registered_external_artifact


def serve_registered_external_artifact(handler, raw_path: str) -> bool:
    row = registered_external_artifact(raw_path)
    if row is None:
        return False
    opened = open_external_regular_file(row["path"])
    if opened is None:
        return False
    fd, path, _file_stat = opened
    try:
        from api.routes import MIME_MAP, _serve_file_bytes

        mime = MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
        _serve_file_bytes(handler, path, mime, None, "no-store", opened_fd=fd)
        fd = None
        return True
    finally:
        close_fd_quietly(fd)
