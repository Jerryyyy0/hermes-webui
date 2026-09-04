"""Version tracking for SkillHub-installed skills.

Shares ``STATE_DIR / session_manifest.db`` with skill_publish tables.
Same connection conventions as ``integration.skill_publish.store``: short
transactions, WAL, idempotent ``_ensure_schema`` per connection.
All functions accept ``db_path=None`` for test injection.

Version history is not persisted locally — the upstream SkillHub API is the
source of truth for version history and change logs. This module only tracks
current installed / upstream versions for update checks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from api.config import STATE_DIR
from integration.skill_publish.version_utils import now, semver_gt

_log = logging.getLogger(__name__)

DB_FILENAME = "session_manifest.db"

_CHANGE_LOG_TYPES = {"新增", "优化", "删除", "修复"}


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
        CREATE TABLE IF NOT EXISTS skill_install_versions (
          catalog_name TEXT PRIMARY KEY,
          display_name TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '',
          local_version TEXT NOT NULL DEFAULT '',
          upstream_version TEXT NOT NULL DEFAULT '',
          upstream_change_logs TEXT NOT NULL DEFAULT '[]',
          upstream_change_log_updated_at TEXT NOT NULL DEFAULT '',
          upstream_checked_at REAL,
          upstream_unreachable INTEGER NOT NULL DEFAULT 0,
          installed_profiles TEXT NOT NULL DEFAULT '[]',
          installed_at REAL NOT NULL,
          upgraded_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Changelog normalization
# ---------------------------------------------------------------------------

def normalize_change_logs(raw: Any) -> list[dict]:
    """Validate and normalize upstream changelog into ``[{type, changeLog}]``.

    Returns a clean list (max 20 entries, each changeLog ≤200 chars, valid type).
    Invalid entries are silently dropped.
    """
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        cl = str(item.get("changeLog") or item.get("change_log") or "").strip()
        if t not in _CHANGE_LOG_TYPES or not cl:
            continue
        if len(cl) > 200:
            cl = cl[:200]
        result.append({"type": t, "changeLog": cl})
        if len(result) >= 20:
            break
    return result


def _serialize_change_logs(change_logs: list[dict]) -> str:
    return json.dumps(change_logs, ensure_ascii=False)


def _deserialize_change_logs(raw: str) -> list[dict]:
    try:
        val = json.loads(raw)
        return normalize_change_logs(val) if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _version_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["upstream_change_logs"] = _deserialize_change_logs(
        d.get("upstream_change_logs", "[]") or "[]"
    )
    d["installed_profiles"] = json.loads(
        d.get("installed_profiles", "[]") or "[]"
    )
    return d


# ---------------------------------------------------------------------------
# CRUD — status table
# ---------------------------------------------------------------------------

def get(catalog_name: str, *, db_path=None) -> dict | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM skill_install_versions WHERE catalog_name = ?",
            (catalog_name,),
        ).fetchone()
    return _version_row_to_dict(row) if row else None


def list_all(*, db_path=None) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_install_versions ORDER BY catalog_name"
        ).fetchall()
    return [_version_row_to_dict(r) for r in rows]


def list_upgradable(*, db_path=None) -> list[dict]:
    """Return installed skills that have a newer upstream version.

    Skills whose upstream is marked unreachable (e.g. delisted from the
    market, or transient upstream downtime) are excluded so they don't
    show a permanent "upgradable" red dot.
    """
    all_rows = list_all(db_path=db_path)
    result = []
    for row in all_rows:
        if row.get("upstream_unreachable"):
            continue
        upstream = str(row.get("upstream_version") or "").strip()
        local = str(row.get("local_version") or "").strip()
        if upstream and local and semver_gt(upstream, local):
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# record_install / record_upgrade
# ---------------------------------------------------------------------------

def record_install(
    catalog_name: str,
    *,
    display_name: str = "",
    category: str = "",
    local_version: str = "",
    change_logs: list[dict] | None = None,
    published_at: str = "",
    profile: str = "default",
    dir_name: str = "",
    db_path=None,
) -> None:
    """UPSERT the status row for a newly installed skill."""
    t = now()
    clean_logs = normalize_change_logs(change_logs)
    profiles_json = json.dumps([{"profile": profile, "dir_name": dir_name}], ensure_ascii=False)

    with closing(_connect(db_path)) as conn:
        existing = conn.execute(
            "SELECT installed_profiles FROM skill_install_versions WHERE catalog_name = ?",
            (catalog_name,),
        ).fetchone()

        if existing:
            existing_profiles = json.loads(existing["installed_profiles"] or "[]")
            if not any(p.get("profile") == profile for p in existing_profiles):
                existing_profiles.append({"profile": profile, "dir_name": dir_name})
            conn.execute(
                """
                UPDATE skill_install_versions SET
                  display_name = COALESCE(NULLIF(?, ''), display_name),
                  category = COALESCE(NULLIF(?, ''), category),
                  local_version = COALESCE(NULLIF(?, ''), local_version),
                  installed_profiles = ?,
                  updated_at = ?
                WHERE catalog_name = ?
                """,
                (
                    display_name, category, local_version,
                    json.dumps(existing_profiles, ensure_ascii=False),
                    t, catalog_name,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO skill_install_versions
                  (catalog_name, display_name, category, local_version,
                   installed_profiles, installed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (catalog_name, display_name, category, local_version,
                 profiles_json, t, t, t),
            )
        conn.commit()


def record_upgrade(
    catalog_name: str,
    *,
    new_version: str,
    change_logs: list[dict] | None = None,
    published_at: str = "",
    action: str = "upgrade",
    db_path=None,
) -> None:
    """Update local_version + upgraded_at on the status row after an upgrade."""
    t = now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE skill_install_versions SET
              local_version = ?, upgraded_at = ?, updated_at = ?
            WHERE catalog_name = ?
            """,
            (new_version, t, t, catalog_name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# refresh_upstream / mark_upstream_unreachable
# ---------------------------------------------------------------------------

def refresh_upstream(
    catalog_name: str,
    *,
    upstream_version: str,
    change_logs: list[dict] | None = None,
    change_log_updated_at: str = "",
    published_at: str = "",
    db_path=None,
) -> None:
    """Write upstream version + changelog on the status row, clear unreachable."""
    t = now()
    clean_logs = normalize_change_logs(change_logs)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE skill_install_versions SET
              upstream_version = ?,
              upstream_change_logs = ?,
              upstream_change_log_updated_at = ?,
              upstream_checked_at = ?,
              upstream_unreachable = 0,
              updated_at = ?
            WHERE catalog_name = ?
            """,
            (
                upstream_version,
                _serialize_change_logs(clean_logs),
                change_log_updated_at,
                t, t, catalog_name,
            ),
        )
        conn.commit()


def mark_upstream_unreachable(catalog_name: str, *, db_path=None) -> None:
    """Flag a skill's upstream as unreachable (preserves old version info)."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE skill_install_versions SET upstream_unreachable = 1, updated_at = ?"
            " WHERE catalog_name = ?",
            (now(), catalog_name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

def remove(catalog_name: str, *, db_path=None) -> None:
    """Delete a skill's version tracking row."""
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "DELETE FROM skill_install_versions WHERE catalog_name = ?",
            (catalog_name,),
        )
        conn.commit()
