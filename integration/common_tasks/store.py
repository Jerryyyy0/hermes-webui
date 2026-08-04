"""Per-profile SQLite store for common tasks.

Lives at ``<profile_home>/common_tasks.db`` alongside ``assistant_bubbles.json``.
Holds two tables: ``tasks`` (seed + mined rows) and ``mining_state`` (KV).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DB_FILENAME = "common_tasks.db"

_SOURCE_SEED = "seed"
_SOURCE_MINED = "mined"


def db_path(profile_path: Path | str) -> Path:
    return Path(profile_path) / DB_FILENAME


def _connect(profile_path: Path | str) -> sqlite3.Connection:
    path = db_path(profile_path)
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
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_rank ON tasks(source, query_count DESC)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mining_state (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
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


def read_all(profile_path: Path | str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return (tasks, state). Empty tuple-shaped result if DB missing or broken."""
    path = db_path(profile_path)
    if not path.exists():
        return [], {}
    try:
        with closing(_connect(profile_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY source ASC, query_count DESC, id ASC"
            ).fetchall()
            tasks = [_row_to_dict(row) for row in rows]
            state_rows = conn.execute(
                "SELECT key, value FROM mining_state"
            ).fetchall()
            state = {row["key"]: row["value"] for row in state_rows}
        return tasks, state
    except sqlite3.Error:
        return [], {}


def write_seed_tasks(
    profile_path: Path | str,
    tasks: list[dict[str, Any]],
) -> None:
    """Replace all seed rows with ``tasks`` and stamp ``seed_generated_at``."""
    now = time.time()
    path = db_path(profile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(profile_path)) as conn:
        with conn:
            conn.execute("DELETE FROM tasks WHERE source = ?", (_SOURCE_SEED,))
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        title, description, trigger_language,
                        query_count, source, members_json,
                        fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
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
            conn.execute(
                """
                INSERT INTO mining_state(key, value) VALUES ('seed_generated_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(now),),
            )
            # Clear retry_after / last_error on success
            _set_state(conn, "retry_after", "")
            _set_state(conn, "last_error", "")
            _set_state(conn, "last_attempt_at", str(now))


def write_seed_placeholder(
    profile_path: Path | str,
    tasks: list[dict[str, Any]],
    *,
    retry_after: float,
    last_error: str = "",
) -> None:
    """Write fallback seed rows as placeholder on LLM failure.

    Mirrors ``assistant_bubbles.generation._write_failure``: does NOT stamp
    ``seed_generated_at``, so the next ``enqueue_missing_or_stale`` after
    ``retry_after`` expires will retry seed generation. Sets ``retry_after``
    and ``last_error`` so the API cooldowns and surfaces the failure reason.
    """
    now = time.time()
    path = db_path(profile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(profile_path)) as conn:
        with conn:
            conn.execute("DELETE FROM tasks WHERE source = ?", (_SOURCE_SEED,))
            for task in tasks:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        title, description, trigger_language,
                        query_count, source, members_json,
                        fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
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
            _set_state(conn, "last_attempt_at", str(now))
            _set_state(conn, "retry_after", str(retry_after))
            _set_state(conn, "last_error", last_error[:200])
            # Explicitly clear seed_generated_at so retry logic fires
            _set_state(conn, "seed_generated_at", "")


def replace_mined_tasks(
    profile_path: Path | str,
    tasks: list[dict[str, Any]],
    fingerprint: str,
    *,
    success: bool = True,
    last_error: str | None = None,
) -> None:
    """Atomically replace mined rows and update mining_state.

    On success: clear old mined, insert new, set last_fingerprint/last_success_at,
    clear retry_after/last_error.
    On failure (success=False, tasks ignored): keep old mined rows, set
    retry_after and last_error, update last_attempt_at.
    """
    now = time.time()
    path = db_path(profile_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(profile_path)) as conn:
        with conn:
            if success:
                conn.execute("DELETE FROM tasks WHERE source = ?", (_SOURCE_MINED,))
                for task in tasks:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            title, description, trigger_language,
                            query_count, source, members_json,
                            fingerprint, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
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
                _set_state(conn, "last_fingerprint", fingerprint)
                _set_state(conn, "last_success_at", str(now))
                _set_state(conn, "last_attempt_at", str(now))
                _set_state(conn, "retry_after", "")
                _set_state(conn, "last_error", "")
            else:
                _set_state(conn, "last_attempt_at", str(now))
                _set_state(conn, "retry_after", str(now + 3))
                if last_error:
                    _set_state(conn, "last_error", last_error[:200])


def update_state(
    profile_path: Path | str,
    key: str,
    value: str | float | None,
) -> None:
    """Upsert a single mining_state key. Empty/None value clears it."""
    path = db_path(profile_path)
    if not path.exists():
        return
    with closing(_connect(profile_path)) as conn:
        with conn:
            _set_state(conn, key, value)


def _set_state(conn: sqlite3.Connection, key: str, value: str | float | None) -> None:
    text = "" if value is None else str(value)
    conn.execute(
        """
        INSERT INTO mining_state(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, text),
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
