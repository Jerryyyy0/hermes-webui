"""Durable profile-aware artifact index for session manifests.

This store is intentionally artifact-only for the first slice. Todos and
references continue to be derived from transcript/tool events in
``api.session_manifest``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from api.config import SESSION_DIR, STATE_DIR

logger = logging.getLogger(__name__)

DB_FILENAME = "session_manifest.db"
ARTIFACT_RECORD_KIND = "artifact"
ASSISTANT_PROSE_SOURCE_TOOL = "assistant_prose"
MEDIA_SOURCE_TOOL = "media"
MANIFEST_PREVIEW_FILE = "file"
MANIFEST_PREVIEW_SKILL = "skill"
_VALID_PREVIEWS = {MANIFEST_PREVIEW_FILE, MANIFEST_PREVIEW_SKILL}
_SAFE_SID_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
)


def _is_safe_session_id(sid: str | None) -> bool:
    return bool(sid and isinstance(sid, str) and all(c in _SAFE_SID_CHARS for c in sid))


def _manifest_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    return (STATE_DIR / DB_FILENAME).expanduser().resolve()


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _manifest_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        logger.debug("failed to configure session manifest sqlite pragmas", exc_info=True)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_manifest_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          lineage_key TEXT NOT NULL,
          profile TEXT NOT NULL DEFAULT '',
          turn_key TEXT NOT NULL,
          record_kind TEXT NOT NULL,
          path TEXT NOT NULL,
          preview TEXT NOT NULL,
          source_tool TEXT NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(lineage_key, profile, turn_key, record_kind, path)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_manifest_records_session
        ON session_manifest_records(session_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_manifest_records_lineage_profile
        ON session_manifest_records(lineage_key, profile)
        """
    )
    conn.commit()


def _read_sidecar_metadata(sid: str | None) -> dict[str, Any] | None:
    if not _is_safe_session_id(sid):
        return None
    path = SESSION_DIR / f"{sid}.json"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Session metadata is written before the large messages array. Prefer a
        # prefix parse to keep lineage checks cheap; fall back to full JSON when
        # the prefix is not enough.
        marker = '\n  "messages":'
        if marker in text:
            prefix = text.split(marker, 1)[0].rstrip()
            if prefix.endswith(","):
                prefix = prefix[:-1]
            try:
                parsed = json.loads(prefix + "\n}")
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _session_row_from_state_db(sid: str | None) -> dict[str, Any] | None:
    if not _is_safe_session_id(sid):
        return None
    try:
        from api.models import _active_state_db_path
    except Exception:
        return None
    db_path = _active_state_db_path()
    if not db_path.exists():
        return None
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "id" not in cols:
                return None
            selected = [
                "id",
                "parent_session_id" if "parent_session_id" in cols else "NULL AS parent_session_id",
                "end_reason" if "end_reason" in cols else "NULL AS end_reason",
            ]
            row = conn.execute(
                f"SELECT {', '.join(selected)} FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        logger.debug("failed to read session lineage from state.db", exc_info=True)
        return None


def _is_compression_parent_child(parent_id: str, child_id: str, child_meta: dict[str, Any]) -> bool:
    parent_meta = _read_sidecar_metadata(parent_id)
    if parent_meta and bool(parent_meta.get("pre_compression_snapshot")):
        return True

    parent_row = _session_row_from_state_db(parent_id)
    child_row = _session_row_from_state_db(child_id)
    if child_row and str(child_row.get("parent_session_id") or "") != parent_id:
        return False
    parent_reason = str((parent_row or {}).get("end_reason") or "").strip().lower()
    child_reason = str((child_row or {}).get("end_reason") or "").strip().lower()
    return parent_reason == "compression" or child_reason == "compression"


def _session_meta(session) -> dict[str, Any]:
    sid = str(getattr(session, "session_id", "") or "").strip()
    meta = _read_sidecar_metadata(sid) or {}
    if not meta:
        meta = {
            "session_id": sid,
            "parent_session_id": getattr(session, "parent_session_id", None),
            "pre_compression_snapshot": bool(getattr(session, "pre_compression_snapshot", False)),
        }
    return meta


def resolve_manifest_lineage_key(session) -> str:
    """Return the compression lineage root for ``session`` or its own id."""
    sid = str(getattr(session, "session_id", "") or "").strip()
    if not _is_safe_session_id(sid):
        return sid
    current_id = sid
    current_meta = _session_meta(session)
    seen = {current_id}
    root_id = sid
    for _ in range(20):
        parent_id = str(current_meta.get("parent_session_id") or "").strip()
        if not parent_id or parent_id in seen or not _is_safe_session_id(parent_id):
            break
        if not _is_compression_parent_child(parent_id, current_id, current_meta):
            break
        root_id = parent_id
        seen.add(parent_id)
        current_id = parent_id
        current_meta = _read_sidecar_metadata(parent_id) or {}
    return root_id


def _profile_for_session(session) -> str:
    return str(getattr(session, "profile", "") or "").strip()


def _normalize_source_tool(value: Any) -> str:
    text = str(value or "").strip()
    return text or ASSISTANT_PROSE_SOURCE_TOOL


def _normalize_preview(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in _VALID_PREVIEWS else MANIFEST_PREVIEW_FILE


def _normalize_record(session, turn_key: str, row: dict[str, Any], record_kind: str) -> dict[str, Any] | None:
    if record_kind != ARTIFACT_RECORD_KIND:
        raise ValueError("session manifest store v1 only supports artifact records")
    path = str(row.get("path") or "").strip()
    tk = str(turn_key or row.get("turn_key") or "").strip()
    if not path or not tk:
        return None
    return {
        "session_id": str(getattr(session, "session_id", "") or "").strip(),
        "lineage_key": resolve_manifest_lineage_key(session),
        "profile": _profile_for_session(session),
        "turn_key": tk,
        "record_kind": ARTIFACT_RECORD_KIND,
        "path": path,
        "preview": _normalize_preview(row.get("preview")),
        "source_tool": _normalize_source_tool(row.get("source_tool")),
    }


def upsert_manifest_records(
    session,
    turn_key: str,
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    record_kind: str = ARTIFACT_RECORD_KIND,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Insert or update artifact rows for one turn.

    Row-level profile values are intentionally ignored; session.profile is the
    single source of truth for profile attribution.
    """
    records: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        record = _normalize_record(session, turn_key, row, record_kind)
        if record:
            records.append(record)
    if not records:
        return []

    now = time.time()
    try:
        with closing(_connect(db_path)) as conn:
            with conn:
                for record in records:
                    conn.execute(
                        """
                        INSERT INTO session_manifest_records (
                          session_id, lineage_key, profile, turn_key, record_kind,
                          path, preview, source_tool, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(lineage_key, profile, turn_key, record_kind, path)
                        DO UPDATE SET
                          session_id = excluded.session_id,
                          preview = excluded.preview,
                          source_tool = excluded.source_tool,
                          updated_at = excluded.updated_at
                        """,
                        (
                            record["session_id"],
                            record["lineage_key"],
                            record["profile"],
                            record["turn_key"],
                            record["record_kind"],
                            record["path"],
                            record["preview"],
                            record["source_tool"],
                            now,
                            now,
                        ),
                    )
    except (sqlite3.Error, OSError, ValueError):
        logger.debug("failed to upsert session manifest records", exc_info=True)
        return []
    return records


def load_manifest_records(
    session,
    *,
    include_lineage: bool = True,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    sid = str(getattr(session, "session_id", "") or "").strip()
    if not _is_safe_session_id(sid):
        return []
    profile = _profile_for_session(session)
    lineage_key = resolve_manifest_lineage_key(session)
    where = "lineage_key = ? AND profile = ?" if include_lineage else "session_id = ? AND profile = ?"
    params = (lineage_key if include_lineage else sid, profile)
    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, lineage_key, profile, turn_key, record_kind,
                       path, preview, source_tool, created_at, updated_at
                FROM session_manifest_records
                WHERE {where} AND record_kind = ?
                ORDER BY turn_key ASC, path ASC
                """,
                (*params, ARTIFACT_RECORD_KIND),
            ).fetchall()
            return [dict(row) for row in rows]
    except (sqlite3.Error, OSError):
        logger.debug("failed to load session manifest records", exc_info=True)
        return []


def get_artifact_profile_index(
    *,
    db_path: Path | str | None = None,
) -> dict[str, str]:
    """Cross-session workspace-relative path → profile index.

    Reads artifact records directly from ``session_manifest.db``. For each
    path, the profile of the record with the greatest ``updated_at`` wins,
    matching the previous ``artifact_profiles.py`` semantics. Paths whose
    winning profile is empty (sessions without a profile) are excluded so the
    index only maps paths to real profile labels.

    The store does not record workspace per row; workspace scoping is left to
    the caller (paths that do not resolve to real files under the target
    workspace are dropped by the caller's file collection step).
    """
    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT path, profile, MAX(updated_at) AS latest
                FROM session_manifest_records
                WHERE record_kind = ? AND preview = ?
                GROUP BY path, profile
                """,
                (ARTIFACT_RECORD_KIND, MANIFEST_PREVIEW_FILE),
            ).fetchall()
    except (sqlite3.Error, OSError):
        logger.debug("failed to read artifact profile index", exc_info=True)
        return {}

    latest_by_path: dict[str, tuple[str, float]] = {}
    for row in rows:
        path = str(row["path"] or "").strip()
        profile = str(row["profile"] or "").strip()
        if not path or not profile:
            continue
        updated_at = float(row["latest"] or 0.0)
        current = latest_by_path.get(path)
        if current is None or updated_at > current[1]:
            latest_by_path[path] = (profile, updated_at)
    return {path: profile for path, (profile, _ts) in latest_by_path.items()}


def get_artifact_paths_for_profile(
    profile: str,
    *,
    db_path: Path | str | None = None,
) -> frozenset[str]:
    """Return workspace-relative artifact paths authored under *profile*.

    The profile label is matched exactly (case-sensitive) against the
    ``profile`` column. Empty/None profile returns an empty set.
    """
    profile_norm = str(profile or "").strip()
    if not profile_norm:
        return frozenset()
    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT path
                FROM session_manifest_records
                WHERE record_kind = ? AND preview = ? AND profile = ?
                """,
                (ARTIFACT_RECORD_KIND, MANIFEST_PREVIEW_FILE, profile_norm),
            ).fetchall()
    except (sqlite3.Error, OSError):
        logger.debug("failed to read artifact paths for profile %s", profile_norm, exc_info=True)
        return frozenset()
    return frozenset(str(row["path"] or "").strip() for row in rows if str(row["path"] or "").strip())


def backfill_empty_profile_artifacts(*, db_path: Path | str | None = None) -> dict[str, int]:
    """Patch artifact rows whose profile column is empty when session.profile is set.

    Scans ``session_manifest.db`` for artifact records with ``profile=''``,
    loads each session's metadata, and updates matching rows when
    ``session.profile`` is non-empty. Sessions without a profile are skipped.
    """
    scanned = 0
    patched = 0
    skipped = 0
    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT session_id
                FROM session_manifest_records
                WHERE record_kind = ? AND profile = ''
                """,
                (ARTIFACT_RECORD_KIND,),
            ).fetchall()
    except (sqlite3.Error, OSError):
        logger.debug("failed to list sessions with empty profile artifacts", exc_info=True)
        return {"scanned": 0, "patched": 0, "skipped": 0}

    from api.models import Session

    session_ids = [
        str(row["session_id"] or "").strip()
        for row in rows
        if _is_safe_session_id(str(row["session_id"] or "").strip())
    ]
    scanned = len(session_ids)

    for sid in session_ids:
        session = Session.load_metadata_only(sid)
        if session is None:
            skipped += 1
            continue
        profile = str(getattr(session, "profile", "") or "").strip()
        if not profile:
            skipped += 1
            continue
        try:
            with closing(_connect(db_path)) as conn:
                with conn:
                    cur = conn.execute(
                        """
                        UPDATE session_manifest_records
                        SET profile = ?
                        WHERE session_id = ? AND record_kind = ? AND profile = ''
                        """,
                        (profile, sid, ARTIFACT_RECORD_KIND),
                    )
                    patched += int(cur.rowcount or 0)
        except (sqlite3.Error, OSError):
            logger.debug(
                "failed to backfill profile for session %s",
                sid,
                exc_info=True,
            )
            skipped += 1

    return {"scanned": scanned, "patched": patched, "skipped": skipped}


def backfill_session_artifacts(
    session,
    *,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Extract and persist artifact records for a session lacking them.

    Targets B-class missing profiles: files on disk that have no artifact row
    in the store. Mirrors the streaming pipeline's ``_persist_turn_artifact_paths``
    exactly — per-turn ``_extract_turn_artifact_entries`` (write_file/edit_file/…
    tool args) plus the prose-path scan of the last assistant message (bare
    filenames / relative paths / media tokens delivered in prose), so files
    produced by non-mutation tools or delivered only in assistant prose are
    also captured. Stored path/source_tool/preview/turn_key match the live
    pipeline's format.

    Re-extracts artifact records from messages and upserts them, mirroring the
    streaming pipeline's ``_persist_turn_artifact_paths`` (per-turn
    ``_extract_turn_artifact_entries`` + prose-path scan + disk-text supplement).
    Sessions are never skipped wholesale — even sessions with a ``turn_artifacts``
    dict or existing store rows may have files that the streaming pipeline missed
    (e.g. paths listed in tool results by non-mutation tools).
    ``upsert_manifest_records``'s ON CONFLICT keeps existing rows untouched while
    adding any missing ones; this complements, never conflicts with, the lazy
    ``backfill_from_session_turn_artifacts`` path in ``/api/session/manifest``
    (which only fires when the store has zero rows).
    """
    sid = str(getattr(session, "session_id", "") or "").strip()
    if not _is_safe_session_id(sid):
        return {"written": 0, "skipped": 1, "turns": 0}

    messages = list(getattr(session, "messages", None) or [])
    if not messages:
        return {"written": 0, "skipped": 1, "turns": 0}

    from api.session_manifest import (
        _extract_turn_artifact_entries,
        _message_text,
        _message_turns,
        _paths_from_last_assistant_message,
        _skills_dir_for_session,
        _turn_message_slice,
        filter_existing_turn_artifact_entries,
    )

    workspace = Path(str(getattr(session, "workspace", "") or "")).expanduser().resolve()
    skills_dir = _skills_dir_for_session(session)
    tool_calls = list(getattr(session, "tool_calls", None) or [])

    # Build a workspace disk-file index once for the disk-text supplement pass
    # below. Capped to avoid pathological workspaces; the common case is a few
    # hundred files. Both relative posix paths and basenames are tracked so
    # absolute-path tool results (e.g. "/Users/.../报告.md") can still match
    # via their basename.
    disk_basenames: dict[str, str] = {}
    try:
        from api.workspace import is_workspace_cruft_basename
        for p in workspace.rglob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            if is_workspace_cruft_basename(p.name):
                continue
            disk_basenames.setdefault(p.name, p.relative_to(workspace).as_posix())
    except (OSError, ValueError):
        pass

    turns = _message_turns(messages)
    written = 0
    turns_with_artifacts = 0
    for turn in turns:
        tk = str(turn.get("turn_key") or "").strip()
        if not tk:
            continue
        slc = _turn_message_slice(messages, tk)
        if not slc:
            continue
        entries = _extract_turn_artifact_entries(
            slc,
            tool_calls,
            workspace,
            start_msg_idx=turn.get("start_msg_idx"),
            end_msg_idx=turn.get("end_msg_idx"),
            skills_dir=skills_dir,
        )
        # Mirror _persist_turn_artifact_paths: also scan the last assistant
        # message in the turn for relative-path/bare-filename deliveries (e.g.
        # markdown table cells mentioning `报告.md`). Only paths that actually
        # exist on disk survive the filter below. Dedupe within the turn only.
        last_assistant_text = ""
        for _m in reversed(slc):
            if isinstance(_m, dict) and _m.get("role") == "assistant":
                last_assistant_text = _message_text(_m.get("content"))
                break
        if last_assistant_text:
            entry_paths = {str(e.get("path") or "") for e in entries if isinstance(e, dict)}
            for _pp in _paths_from_last_assistant_message(last_assistant_text, workspace):
                if _pp and _pp not in entry_paths:
                    entries.append({
                        "path": _pp,
                        "source_tool": ASSISTANT_PROSE_SOURCE_TOOL,
                        "preview": MANIFEST_PREVIEW_FILE,
                    })
        # Backfill-only disk-text supplement: scan the whole turn slice (tool
        # results included, not just assistant prose) for any disk file basename.
        # This catches files listed by non-mutation tools (e.g. a glob/read tool
        # returning absolute paths) that the streaming pipeline intentionally
        # skips. ``filter_existing_turn_artifact_entries`` below keeps it safe.
        if disk_basenames:
            entry_paths = {str(e.get("path") or "") for e in entries if isinstance(e, dict)}
            import json as _json
            slice_text = _json.dumps(slc, ensure_ascii=False)
            for basename, rel in disk_basenames.items():
                if rel in entry_paths or basename not in slice_text:
                    continue
                entries.append({
                    "path": rel,
                    "source_tool": ASSISTANT_PROSE_SOURCE_TOOL,
                    "preview": MANIFEST_PREVIEW_FILE,
                })
        entries = filter_existing_turn_artifact_entries(workspace, skills_dir, entries)
        if not entries:
            continue
        turns_with_artifacts += 1
        rows = [
            {
                "path": str(entry.get("path") or "").strip(),
                "source_tool": str(entry.get("source_tool") or ASSISTANT_PROSE_SOURCE_TOOL).strip(),
                "preview": str(entry.get("preview") or MANIFEST_PREVIEW_FILE).strip(),
            }
            for entry in entries
            if isinstance(entry, dict) and entry.get("path")
        ]
        if not rows:
            continue
        try:
            records = upsert_manifest_records(session, tk, rows, db_path=db_path)
            written += len(records)
        except Exception:
            logger.debug("failed to upsert manifest rows for session %s turn %s", sid, tk, exc_info=True)

    return {"written": written, "skipped": 0, "turns": turns_with_artifacts}


def backfill_workspace_artifacts_from_sessions(
    *,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Scan persisted sessions and backfill artifact records for those missing them.

    Iterates session JSON files under ``SESSION_DIR``. For each session, loads
    metadata-only first to filter by a non-empty profile (only profiled
    sessions contribute to the workspace profile index); only when extraction
    is needed does it pay the cost of a full ``Session.load``.
    """
    scanned = 0
    written = 0
    skipped = 0
    sessions_with_artifacts = 0

    from api.models import Session

    try:
        ids = sorted(
            p.stem
            for p in SESSION_DIR.glob("*.json")
            if not p.name.startswith("_")
        )
    except OSError:
        logger.debug("failed to list session dir for workspace artifact backfill", exc_info=True)
        return {"scanned": 0, "written": 0, "skipped": 0, "sessions": 0}

    for sid in ids:
        scanned += 1
        meta = Session.load_metadata_only(sid)
        if meta is None:
            skipped += 1
            continue
        profile = str(getattr(meta, "profile", "") or "").strip()
        if not profile:
            skipped += 1
            continue
        try:
            session = Session.load(sid)
        except Exception:
            logger.debug("failed to full-load session %s for backfill", sid, exc_info=True)
            skipped += 1
            continue
        if session is None:
            skipped += 1
            continue
        try:
            result = backfill_session_artifacts(session, db_path=db_path)
            w = int(result.get("written") or 0)
            if w:
                written += w
                sessions_with_artifacts += 1
            elif int(result.get("skipped") or 0):
                skipped += 1
        except Exception:
            logger.debug("failed to backfill artifacts for session %s", sid, exc_info=True)
            skipped += 1

    return {
        "scanned": scanned,
        "written": written,
        "skipped": skipped,
        "sessions": sessions_with_artifacts,
    }


def delete_session_manifest_records(session_id: str, *, db_path: Path | str | None = None) -> None:
    sid = str(session_id or "").strip()
    if not _is_safe_session_id(sid):
        return
    try:
        with closing(_connect(db_path)) as conn:
            with conn:
                conn.execute("DELETE FROM session_manifest_records WHERE session_id = ?", (sid,))
    except (sqlite3.Error, OSError):
        logger.debug("failed to delete session manifest records for %s", sid, exc_info=True)


def delete_session_manifest_turns(
    session_id: str,
    keep_turn_keys: set[str] | list[str] | tuple[str, ...],
    *,
    db_path: Path | str | None = None,
) -> None:
    sid = str(session_id or "").strip()
    if not _is_safe_session_id(sid):
        return
    keep = sorted({str(key or "").strip() for key in keep_turn_keys or [] if str(key or "").strip()})
    try:
        with closing(_connect(db_path)) as conn:
            with conn:
                if not keep:
                    conn.execute("DELETE FROM session_manifest_records WHERE session_id = ?", (sid,))
                    return
                placeholders = ",".join("?" for _ in keep)
                conn.execute(
                    f"""
                    DELETE FROM session_manifest_records
                    WHERE session_id = ? AND turn_key NOT IN ({placeholders})
                    """,
                    (sid, *keep),
                )
    except (sqlite3.Error, OSError):
        logger.debug("failed to prune session manifest turns for %s", sid, exc_info=True)


def backfill_from_session_turn_artifacts(
    session,
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    raw = getattr(session, "turn_artifacts", None)
    if not isinstance(raw, dict):
        return []
    rows_by_turn: dict[str, list[dict[str, Any]]] = {}
    for turn_key, entries in raw.items():
        tk = str(turn_key or "").strip()
        if not tk or not isinstance(entries, list):
            continue
        rows: list[dict[str, Any]] = []
        for entry in entries:
            if isinstance(entry, str):
                path = entry.strip()
                source_tool = ASSISTANT_PROSE_SOURCE_TOOL
            elif isinstance(entry, dict):
                path = str(entry.get("path") or "").strip()
                source_tool = _normalize_source_tool(entry.get("source_tool"))
            else:
                continue
            if path:
                preview = str(entry.get("preview") or "").strip() if isinstance(entry, dict) else ""
                rows.append({
                    "path": path,
                    "source_tool": source_tool,
                    "preview": preview if preview in _VALID_PREVIEWS else MANIFEST_PREVIEW_FILE,
                })
        if rows:
            rows_by_turn[tk] = rows

    written: list[dict[str, Any]] = []
    for turn_key, rows in rows_by_turn.items():
        written.extend(upsert_manifest_records(session, turn_key, rows, db_path=db_path))
    return written
