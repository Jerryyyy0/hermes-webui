"""Cross-session workspace artifact path → profile index for integration workspace files."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from api.config import SESSION_DIR, SESSION_INDEX_FILE
from api.session_manifest import MANIFEST_PREVIEW_FILE, build_session_manifest

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}


def _cache_key_for_workspace(workspace_root: Path) -> str:
    return str(workspace_root.expanduser().resolve())


def _session_cache_key() -> tuple[int | None, int | None]:
    index_mtime_ns: int | None = None
    dir_mtime_ns: int | None = None
    try:
        index_mtime_ns = SESSION_INDEX_FILE.stat().st_mtime_ns
    except OSError:
        pass
    try:
        dir_mtime_ns = SESSION_DIR.stat().st_mtime_ns
    except OSError:
        pass
    return index_mtime_ns, dir_mtime_ns


def _load_session_index_rows() -> list[dict[str, Any]]:
    if not SESSION_INDEX_FILE.exists():
        return []
    try:
        raw = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _session_row_skip(row: dict[str, Any]) -> bool:
    message_count = row.get('message_count')
    try:
        count = int(message_count or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return False
    if row.get('active_stream_id'):
        return False
    if row.get('pending_user_message'):
        return False
    return True


def _row_matches_workspace(row: dict[str, Any], workspace_root: Path) -> bool:
    ws_raw = str(row.get('workspace') or '').strip()
    if not ws_raw:
        return True
    try:
        return Path(ws_raw).expanduser().resolve() == workspace_root
    except (TypeError, ValueError, OSError):
        return False


def _session_updated_at(session) -> float:
    for attr in ('updated_at', 'last_message_at', 'created_at'):
        value = getattr(session, attr, None)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _artifact_paths_for_session(session, workspace_root: Path) -> list[tuple[str, str]]:
    try:
        session_ws = Path(str(session.workspace)).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return []
    if session_ws != workspace_root:
        return []
    profile = str(getattr(session, 'profile', None) or '').strip()
    if not profile:
        return []
    try:
        manifest = build_session_manifest(session)
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for row in manifest.get('artifacts') or []:
        if not isinstance(row, dict):
            continue
        if str(row.get('preview') or '').strip() != MANIFEST_PREVIEW_FILE:
            continue
        path = str(row.get('path') or '').strip()
        row_profile = str(row.get('profile') or '').strip() or profile
        if path and row_profile:
            out.append((path, row_profile))
    return out


def _index_from_path_sources(path_sources: dict[str, dict[str, tuple[str, float]]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for path, owners in path_sources.items():
        if not owners:
            continue
        profile, _updated_at = max(owners.values(), key=lambda item: item[1])
        index[path] = profile
    return index


def _by_session_from_path_sources(
    path_sources: dict[str, dict[str, tuple[str, float]]],
) -> dict[str, set[str]]:
    by_session: dict[str, set[str]] = {}
    for path, owners in path_sources.items():
        for sid in owners:
            by_session.setdefault(sid, set()).add(path)
    return by_session


def _store_cache_entry(
    ws_key: str,
    *,
    cache_key: tuple[int | None, int | None],
    path_sources: dict[str, dict[str, tuple[str, float]]],
) -> dict[str, str]:
    index = _index_from_path_sources(path_sources)
    _CACHE[ws_key] = {
        'cache_key': cache_key,
        'index': dict(index),
        'by_session': {
            sid: set(paths)
            for sid, paths in _by_session_from_path_sources(path_sources).items()
        },
        'path_sources': {
            path: {sid: (profile, updated_at) for sid, (profile, updated_at) in owners.items()}
            for path, owners in path_sources.items()
        },
    }
    return index


def _rebuild_path_sources(root: Path) -> dict[str, dict[str, tuple[str, float]]]:
    from api.models import Session

    path_sources: dict[str, dict[str, tuple[str, float]]] = {}
    for row in _load_session_index_rows():
        if not isinstance(row, dict):
            continue
        if _session_row_skip(row):
            continue
        if not _row_matches_workspace(row, root):
            continue
        sid = str(row.get('session_id') or '').strip()
        if not sid:
            continue
        session = Session.load(sid)
        if session is None:
            continue
        updated_at = _session_updated_at(session)
        for path, profile in _artifact_paths_for_session(session, root):
            path_sources.setdefault(path, {})[sid] = (profile, updated_at)
    return path_sources


def build_workspace_artifact_profile_index(
    *,
    workspace_root: Path,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Build workspace-relative path → profile (latest session.updated_at wins)."""
    root = workspace_root.expanduser().resolve()
    path_sources = _rebuild_path_sources(root)
    index = _index_from_path_sources(path_sources)
    by_session = _by_session_from_path_sources(path_sources)
    return index, by_session


def get_workspace_artifact_profile_index(workspace_root: Path) -> dict[str, str]:
    """Return cached path→profile index; rebuild on session index/dir mtime change."""
    root = workspace_root.expanduser().resolve()
    ws_key = _cache_key_for_workspace(root)
    cache_key = _session_cache_key()

    with _CACHE_LOCK:
        cached = _CACHE.get(ws_key)
        if cached and cached.get('cache_key') == cache_key:
            index = cached.get('index')
            if isinstance(index, dict):
                return dict(index)

    path_sources = _rebuild_path_sources(root)
    with _CACHE_LOCK:
        index = _store_cache_entry(ws_key, cache_key=cache_key, path_sources=path_sources)
    return dict(index)


def update_workspace_artifact_profile_for_session(session, *, workspace_root: Path) -> None:
    """Merge one session's artifact paths into the cached index."""
    root = workspace_root.expanduser().resolve()
    ws_key = _cache_key_for_workspace(root)
    sid = str(getattr(session, 'session_id', None) or '').strip()
    if not sid:
        return

    updated_at = _session_updated_at(session)
    owners_for_session = {
        path: (profile, updated_at)
        for path, profile in _artifact_paths_for_session(session, root)
    }

    with _CACHE_LOCK:
        cached = _CACHE.get(ws_key)
        if not cached:
            return

        path_sources = {
            path: dict(owners)
            for path, owners in (cached.get('path_sources') or {}).items()
        }
        for owners in path_sources.values():
            owners.pop(sid, None)
        path_sources = {
            path: owners
            for path, owners in path_sources.items()
            if owners
        }
        for path, (profile, ts) in owners_for_session.items():
            path_sources.setdefault(path, {})[sid] = (profile, ts)

        cache_key = _session_cache_key()
        _store_cache_entry(ws_key, cache_key=cache_key, path_sources=path_sources)


def invalidate_workspace_artifact_profile_index(workspace_root: Path | None = None) -> None:
    """Drop cached index (all roots or one workspace)."""
    with _CACHE_LOCK:
        if workspace_root is None:
            _CACHE.clear()
            return
        ws_key = _cache_key_for_workspace(workspace_root)
        _CACHE.pop(ws_key, None)
