"""In-memory workspace file index with event-driven invalidation."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from api.workspace import _collect_workspace_file_entries

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_INFLIGHT: dict[str, threading.Event] = {}


def _cache_key(workspace: Path, rel: str) -> str:
    root = workspace.expanduser().resolve()
    rel_norm = (rel or ".").strip() or "."
    return f"{root}|{rel_norm}"


def invalidate_workspace_file_index(
    workspace_root: Path | None = None,
    rel: str | None = None,
) -> None:
    """Drop cached file index (all roots, one workspace, or one subtree)."""
    with _CACHE_LOCK:
        if workspace_root is None:
            _CACHE.clear()
            return
        ws_prefix = str(workspace_root.expanduser().resolve()) + "|"
        if rel is None:
            for key in list(_CACHE):
                if key.startswith(ws_prefix):
                    _CACHE.pop(key, None)
            return
        _CACHE.pop(_cache_key(workspace_root, rel), None)


def _rebuild_entries(workspace: Path, rel: str) -> list[dict]:
    return _collect_workspace_file_entries(workspace, rel)


def get_workspace_file_entries(
    workspace: Path,
    rel: str = ".",
    *,
    force_refresh: bool = False,
) -> list[dict]:
    """Return cached workspace file entries, rebuilding on miss or *force_refresh*."""
    if force_refresh:
        invalidate_workspace_file_index(workspace, rel)

    key = _cache_key(workspace, rel)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            entries = cached.get("entries")
            if isinstance(entries, list):
                return list(entries)
        wait_event = _INFLIGHT.get(key)
        if wait_event is None:
            wait_event = threading.Event()
            _INFLIGHT[key] = wait_event
            is_builder = True
        else:
            is_builder = False

    if not is_builder:
        wait_event.wait()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                entries = cached.get("entries")
                if isinstance(entries, list):
                    return list(entries)
        return _rebuild_entries(workspace, rel)

    try:
        entries = _rebuild_entries(workspace, rel)
        with _CACHE_LOCK:
            _CACHE[key] = {"entries": list(entries)}
            done = _INFLIGHT.pop(key, None)
            if done is not None:
                done.set()
        return list(entries)
    except Exception:
        with _CACHE_LOCK:
            done = _INFLIGHT.pop(key, None)
            if done is not None:
                done.set()
        raise
