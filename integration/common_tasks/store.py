"""Profile-aware common tasks store backed by the global session_manifest.db.

Tables live alongside ``session_manifest_records`` in
``STATE_DIR / session_manifest.db`` and are keyed by ``profile`` so multiple
profiles share one DB file. Two tables:

  - ``common_tasks(profile, title, description, trigger_language, query_count,
                   source, members_json, fingerprint, created_at, updated_at)``
  - ``common_tasks_mining_state(profile, key, value)``

Mirrors the schema of the previous per-profile ``common_tasks.db`` minus the
file-per-profile layout.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

SCHEMA_VERSION = 1
DB_FILENAME = "session_manifest.db"

_SOURCE_SEED = "seed"
_SOURCE_MINED = "mined"


def _db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser().resolve()
    return (STATE_DIR / DB_FILENAME).expanduser().resolve()


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS common_tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          profile TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          trigger_language TEXT NOT NULL DEFAULT '',
          query_count INTEGER NOT NULL DEFAULT 0,
          source TEXT NOT NULL,
          members_json TEXT NOT NULL DEFAULT '[]',
          fingerprint TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_common_tasks_profile_source "
        "ON common_tasks(profile, source)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_common_tasks_profile_rank "
        "ON common_tasks(profile, source, query_count DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS common_tasks_mining_state (
          profile TEXT NOT NULL,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          PRIMARY KEY (profile, key)
        )
        """
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "profile": row["profile"],
        "title": row["title"],
        "description": row["description"],
        "trigger_language": row["trigger_language"],
        "query_count": row["query_count"],
        "source": row["source"],
        "members_json": row["members_json"],
        "fingerprint": row["fingerprint"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_profile(profile: str) -> str:
    return str(profile or "").strip()


def read_all(
    profile: str,
    *,
    db_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return (tasks, state). Empty tuple-shaped result if DB missing or broken."""
    profile_norm = _normalize_profile(profile)
    if not profile_norm:
        return [], {}
    path = _db_path(db_path)
    if not path.exists():
        return [], {}
    try:
        with closing(_connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM common_tasks WHERE profile = ? "
                "ORDER BY source ASC, query_count DESC, id ASC",
                (profile_norm,),
            ).fetchall()
            tasks = [_row_to_dict(row) for row in rows]
            state_rows = conn.execute(
                "SELECT key, value FROM common_tasks_mining_state WHERE profile = ?",
                (profile_norm,),
            ).fetchall()
            state = {row["key"]: row["value"] for row in state_rows}
        return tasks, state
    except sqlite3.Error:
        return [], {}


def write_seed_tasks(
    profile: str,
    tasks: list[dict[str, Any]],
    *,
    db_path: Path | str | None = None,
) -> None:
    """Replace all seed rows with ``tasks`` and stamp ``seed_generated_at``."""
    profile_norm = _normalize_profile(profile)
    if not profile_norm:
        return
    now = time.time()
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "DELETE FROM common_tasks WHERE profile = ? AND source = ?",
                (profile_norm, _SOURCE_SEED),
            )
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO common_tasks (
                        profile, title, description, trigger_language,
                        query_count, source, members_json,
                        fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        profile_norm,
                        str(task.get("title") or "")[:15],
                        str(task.get("description") or "")[:50],
                        str(task.get("trigger_language") or "")[:30],
                        int(task.get("query_count") or 0),
                        _SOURCE_SEED,
                        "[]",
                        now,
                        now,
                    ),
                )
            _set_state(conn, profile_norm, "seed_generated_at", str(now))
            _set_state(conn, profile_norm, "last_attempt_at", str(now))
            _set_state(conn, profile_norm, "retry_after", "")
            _set_state(conn, profile_norm, "last_error", "")


def write_seed_placeholder(
    profile: str,
    tasks: list[dict[str, Any]],
    *,
    retry_after: float,
    last_error: str = "",
    db_path: Path | str | None = None,
) -> None:
    """Write fallback seed rows as placeholder on LLM failure.

    Mirrors ``assistant_bubbles.generation._write_failure``: does NOT stamp
    ``seed_generated_at``, so the next ``enqueue_missing_or_stale`` after
    ``retry_after`` expires will retry seed generation. Sets ``retry_after``
    and ``last_error`` so the API cooldowns and surfaces the failure reason.
    """
    profile_norm = _normalize_profile(profile)
    if not profile_norm:
        return
    now = time.time()
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.execute(
                "DELETE FROM common_tasks WHERE profile = ? AND source = ?",
                (profile_norm, _SOURCE_SEED),
            )
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO common_tasks (
                        profile, title, description, trigger_language,
                        query_count, source, members_json,
                        fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        profile_norm,
                        str(task.get("title") or "")[:15],
                        str(task.get("description") or "")[:50],
                        str(task.get("trigger_language") or "")[:30],
                        int(task.get("query_count") or 0),
                        _SOURCE_SEED,
                        "[]",
                        now,
                        now,
                    ),
                )
            _set_state(conn, profile_norm, "last_attempt_at", str(now))
            _set_state(conn, profile_norm, "retry_after", str(retry_after))
            _set_state(conn, profile_norm, "last_error", last_error[:200])
            # Explicitly clear seed_generated_at so retry logic fires
            _set_state(conn, profile_norm, "seed_generated_at", "")


def replace_mined_tasks(
    profile: str,
    tasks: list[dict[str, Any]],
    fingerprint: str,
    *,
    success: bool = True,
    last_error: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Atomically replace mined rows and update mining_state.

    On success: clear old mined, insert new, set last_fingerprint/last_success_at,
    clear retry_after/last_error.
    On failure (success=False, tasks ignored): keep old mined rows, set
    retry_after and last_error, update last_attempt_at.
    """
    profile_norm = _normalize_profile(profile)
    if not profile_norm:
        return
    now = time.time()
    with closing(_connect(db_path)) as conn:
        with conn:
            if success:
                conn.execute(
                    "DELETE FROM common_tasks WHERE profile = ? AND source = ?",
                    (profile_norm, _SOURCE_MINED),
                )
                for task in tasks:
                    conn.execute(
                        """
                        INSERT INTO common_tasks (
                            profile, title, description, trigger_language,
                            query_count, source, members_json,
                            fingerprint, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            profile_norm,
                            str(task.get("title") or "")[:15],
                            str(task.get("description") or "")[:50],
                            str(task.get("trigger_language") or "")[:30],
                            int(task.get("query_count") or 0),
                            _SOURCE_MINED,
                            str(task.get("members_json") or "[]"),
                            fingerprint,
                            now,
                            now,
                        ),
                    )
                _set_state(conn, profile_norm, "last_fingerprint", fingerprint)
                _set_state(conn, profile_norm, "last_success_at", str(now))
                _set_state(conn, profile_norm, "last_attempt_at", str(now))
                _set_state(conn, profile_norm, "retry_after", "")
                _set_state(conn, profile_norm, "last_error", "")
            else:
                _set_state(conn, profile_norm, "last_attempt_at", str(now))
                _set_state(conn, profile_norm, "retry_after", str(now + 3))
                if last_error:
                    _set_state(conn, profile_norm, "last_error", last_error[:200])


def update_state(
    profile: str,
    key: str,
    value: str | float | None,
    *,
    db_path: Path | str | None = None,
) -> None:
    """Upsert a single mining_state key. Empty/None value clears it."""
    profile_norm = _normalize_profile(profile)
    if not profile_norm:
        return
    path = _db_path(db_path)
    if not path.exists():
        return
    with closing(_connect(db_path)) as conn:
        with conn:
            _set_state(conn, profile_norm, key, value)


def _set_state(conn: sqlite3.Connection, profile: str, key: str, value: str | float | None) -> None:
    text = "" if value is None else str(value)
    conn.execute(
        """
        INSERT INTO common_tasks_mining_state(profile, key, value) VALUES (?, ?, ?)
        ON CONFLICT(profile, key) DO UPDATE SET value = excluded.value
        """,
        (profile, key, text),
    )


def pick_top3(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mined first (by query_count DESC), seed fills remaining slots up to 3."""
    mined = [t for t in tasks if t.get("source") == _SOURCE_MINED]
    seed = [t for t in tasks if t.get("source") == _SOURCE_SEED]
    mined.sort(key=lambda t: int(t.get("query_count") or 0), reverse=True)
    out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for task in mined + seed:
        title = str(task.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        out.append({
            "title": task["title"],
            "description": task["description"],
            "trigger_language": task["trigger_language"],
            "query_count": int(task.get("query_count") or 0),
            "source": task["source"],
        })
        if len(out) >= 3:
            break
    return out


def has_mined(tasks: list[dict[str, Any]]) -> bool:
    return any(t.get("source") == _SOURCE_MINED for t in tasks)


def has_seed(tasks: list[dict[str, Any]]) -> bool:
    return any(t.get("source") == _SOURCE_SEED for t in tasks)