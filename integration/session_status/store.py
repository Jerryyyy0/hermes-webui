"""Global WebUI SQLite store for per-profile session read cursors."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

from api.config import STATE_DIR

from .compute import latest_activity_at, to_timestamp

logger = logging.getLogger(__name__)
DB_PATH = STATE_DIR / "session_status.db"
_QUERY_BATCH_SIZE = 200


def normalize_profile_key(profile: object) -> str:
    value = str(profile or "").strip()
    if not value or value == "default":
        return "default"
    try:
        from api.profiles import _is_root_profile

        if _is_root_profile(value):
            return "default"
    except Exception:
        pass
    return value


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path).expanduser().resolve() if db_path is not None else DB_PATH


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_read_state (
          profile TEXT NOT NULL,
          session_id TEXT NOT NULL,
          read_until REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (profile, session_id)
        )
        """
    )
    conn.commit()


def _connect(
    db_path: Path | str | None = None,
    *,
    create: bool = False,
) -> sqlite3.Connection | None:
    path = _db_path(db_path)
    if not create and not path.exists():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if create:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            logger.debug("failed to configure session status WAL", exc_info=True)
        _ensure_schema(conn)
    return conn


def _row_keys(rows: Iterable[dict]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        sid = str(row.get("session_id") or "").strip()
        if not sid:
            continue
        key = (normalize_profile_key(row.get("profile")), sid)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def get_read_until_map(
    rows: Iterable[dict],
    *,
    db_path: Path | str | None = None,
) -> dict[tuple[str, str], float]:
    """Read all requested profile/session cursors through one connection."""

    keys = _row_keys(rows)
    if not keys:
        return {}
    try:
        conn = _connect(db_path, create=False)
        if conn is None:
            return {}
        with closing(conn):
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_read_state'"
            ).fetchone()
            if table is None:
                return {}
            result: dict[tuple[str, str], float] = {}
            for offset in range(0, len(keys), _QUERY_BATCH_SIZE):
                batch = keys[offset:offset + _QUERY_BATCH_SIZE]
                predicates = " OR ".join("(profile = ? AND session_id = ?)" for _ in batch)
                params = [part for key in batch for part in key]
                fetched = conn.execute(
                    f"SELECT profile, session_id, read_until FROM session_read_state WHERE {predicates}",
                    params,
                ).fetchall()
                for row in fetched:
                    result[(str(row["profile"]), str(row["session_id"]))] = to_timestamp(
                        row["read_until"]
                    )
            return result
    except sqlite3.Error:
        logger.debug("failed to read session status cursors", exc_info=True)
        return {}


def advance_read_until(
    profile: object,
    session_id: str,
    observed_at: object,
    *,
    db_path: Path | str | None = None,
) -> float:
    key = normalize_profile_key(profile)
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")
    observed = to_timestamp(observed_at)
    conn = _connect(db_path, create=True)
    if conn is None:
        raise sqlite3.OperationalError("session status database unavailable")
    with closing(conn):
        conn.execute(
            """
            INSERT INTO session_read_state(profile, session_id, read_until)
            VALUES (?, ?, ?)
            ON CONFLICT(profile, session_id) DO UPDATE SET
              read_until = MAX(session_read_state.read_until, excluded.read_until)
            """,
            (key, sid, observed),
        )
        conn.commit()
        row = conn.execute(
            "SELECT read_until FROM session_read_state WHERE profile = ? AND session_id = ?",
            (key, sid),
        ).fetchone()
        return to_timestamp(row["read_until"] if row else observed)


def mark_session_read(
    profile: object,
    session,
    *,
    db_path: Path | str | None = None,
) -> dict[str, object]:
    row = session.compact()
    observed_at = latest_activity_at(row)
    read_until = advance_read_until(
        profile,
        str(session.session_id),
        observed_at,
        db_path=db_path,
    )
    return {
        "ok": True,
        "session_id": str(session.session_id),
        "read_until": read_until,
    }


def delete_profile_read_state(
    profile: object,
    *,
    db_path: Path | str | None = None,
) -> None:
    conn = _connect(db_path, create=False)
    if conn is None:
        return
    with closing(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_read_state'"
        ).fetchone()
        if table is None:
            return
        conn.execute(
            "DELETE FROM session_read_state WHERE profile = ?",
            (normalize_profile_key(profile),),
        )
        conn.commit()
