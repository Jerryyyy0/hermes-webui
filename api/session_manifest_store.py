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
                rows.append({
                    "path": path,
                    "source_tool": source_tool,
                    "preview": MANIFEST_PREVIEW_FILE,
                })
        if rows:
            rows_by_turn[tk] = rows

    written: list[dict[str, Any]] = []
    for turn_key, rows in rows_by_turn.items():
        written.extend(upsert_manifest_records(session, turn_key, rows, db_path=db_path))
    return written
