"""Skill publish tables in the global session_manifest.db (docs §4).

Shares ``STATE_DIR / session_manifest.db`` with ``session_manifest_records``
and ``common_tasks`` - same connection conventions as
``integration.session_manifest.store`` / ``integration.common_tasks.store``: short
transactions, ``busy_timeout=5000``, WAL, idempotent ``_ensure_schema`` per
connection. All functions accept ``db_path=None`` for test injection.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from api.config import STATE_DIR
from integration.skill_publish.constants import PublishStatus
from integration.skill_publish.version_utils import now

DB_FILENAME = "session_manifest.db"

_APPLICATION_COLUMNS = (
    "id", "skill_name", "display_name", "description", "display_description",
    "category", "tags", "detail_json", "skill_md_content", "application_type",
    "reason", "version", "status", "submitter_account", "submitter_uuid",
    "applicant_name", "applicant_org", "applicant_title", "platform",
    "external_user_id", "upstream_name", "upstream_skill_id",
    "upstream_application_id", "upstream_status", "audit_comment",
    "audit_event_id", "created_at", "updated_at", "submitted_at", "audited_at",
    "hidden", "hidden_at",
)


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
        CREATE TABLE IF NOT EXISTS skill_application_forms (
          id TEXT PRIMARY KEY,
          skill_name TEXT NOT NULL,
          display_name TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          display_description TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '',
          tags TEXT NOT NULL DEFAULT '[]',
          detail_json TEXT NOT NULL DEFAULT '',
          skill_md_content TEXT NOT NULL DEFAULT '',
          application_type TEXT NOT NULL DEFAULT 'publish',
          reason TEXT NOT NULL DEFAULT '',
          version TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft',
          submitter_account TEXT NOT NULL,
          submitter_uuid TEXT NOT NULL DEFAULT '',
          applicant_name TEXT NOT NULL DEFAULT '',
          applicant_org TEXT NOT NULL DEFAULT '',
          applicant_title TEXT NOT NULL DEFAULT '',
          platform TEXT NOT NULL DEFAULT 'hermes-webui',
          external_user_id TEXT NOT NULL DEFAULT '',
          upstream_name TEXT NOT NULL DEFAULT '',
          upstream_skill_id TEXT NOT NULL DEFAULT '',
          upstream_application_id TEXT NOT NULL DEFAULT '',
          upstream_status TEXT NOT NULL DEFAULT '',
          audit_comment TEXT NOT NULL DEFAULT '',
          audit_event_id INTEGER,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          submitted_at REAL,
          audited_at REAL,
          hidden INTEGER NOT NULL DEFAULT 0,
          hidden_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_apps_status ON skill_application_forms(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_apps_submitter"
        " ON skill_application_forms(submitter_account, submitter_uuid)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_apps_skill_version"
        " ON skill_application_forms(skill_name, version)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_apps_upstream_application_id"
        " ON skill_application_forms(upstream_application_id) WHERE upstream_application_id != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_apps_hidden ON skill_application_forms(hidden)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_publish_audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          application_id TEXT NOT NULL,
          action TEXT NOT NULL,
          from_status TEXT NOT NULL DEFAULT '',
          to_status TEXT NOT NULL,
          operator TEXT NOT NULL,
          operator_name TEXT NOT NULL DEFAULT '',
          operator_role TEXT NOT NULL,
          comment TEXT NOT NULL DEFAULT '',
          upstream_application_id TEXT NOT NULL DEFAULT '',
          upstream_event_id INTEGER,
          created_at REAL NOT NULL,
          UNIQUE(upstream_event_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_log_application"
        " ON skill_publish_audit_log(application_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_versions (
          skill_name TEXT NOT NULL,
          version TEXT NOT NULL,
          submitter_account TEXT NOT NULL,
          display_name TEXT NOT NULL DEFAULT '',
          upstream_skill_id TEXT NOT NULL DEFAULT '',
          upstream_status TEXT NOT NULL DEFAULT '',
          upstream_status_updated_at REAL,
          application_id TEXT NOT NULL DEFAULT '',
          release_notes TEXT NOT NULL DEFAULT '',
          is_latest INTEGER NOT NULL DEFAULT 0,
          released_at REAL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          PRIMARY KEY (skill_name, version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_versions_submitter"
        " ON skill_versions(submitter_account)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_versions_latest"
        " ON skill_versions(skill_name, is_latest)"
    )
    conn.commit()


def new_application_id() -> str:
    return f"skp-{uuid.uuid4().hex[:8]}"


def _row_to_application(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in _APPLICATION_COLUMNS}


def create_application(
    *, fields: dict[str, Any], db_path: Path | str | None = None
) -> dict[str, Any]:
    data = {key: fields.get(key, "") for key in _APPLICATION_COLUMNS}
    data["id"] = data["id"] or new_application_id()
    ts = now()
    if not data.get("created_at"):
        data["created_at"] = ts
    if not data.get("updated_at"):
        data["updated_at"] = ts
    if not data.get("status"):
        data["status"] = PublishStatus.DRAFT
    if not data.get("hidden"):
        data["hidden"] = 0
    if data.get("audit_event_id") == "" or data.get("audit_event_id") is None:
        data["audit_event_id"] = None
    cols = ", ".join(_APPLICATION_COLUMNS)
    placeholders = ", ".join("?" for _ in _APPLICATION_COLUMNS)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            f"INSERT INTO skill_application_forms ({cols}) VALUES ({placeholders})",
            tuple(data[key] for key in _APPLICATION_COLUMNS),
        )
        conn.commit()
    return data


def get_application(
    app_id: str, *, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM skill_application_forms WHERE id = ?", (app_id,)
        ).fetchone()
    return _row_to_application(row) if row else None


def update_application(
    app_id: str,
    *,
    fields: dict[str, Any],
    db_path: Path | str | None = None,
) -> bool:
    if not fields:
        return False
    fields = dict(fields)
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE skill_application_forms SET {assignments} WHERE id = ?",
            (*fields.values(), app_id),
        )
        conn.commit()
        return cur.rowcount > 0


_SEARCH_FIELDS = (
    "skill_name", "display_name", "description", "applicant_name", "applicant_org",
)


def list_applications(
    *,
    q: str | None = None,
    application_type: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    clauses = ["hidden = 0"]
    params: list[Any] = []
    if application_type:
        clauses.append("application_type = ?")
        params.append(application_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        like = f"%{q}%"
        clauses.append(
            "(" + " OR ".join(f"{f} LIKE ?" for f in _SEARCH_FIELDS) + ")"
        )
        params.extend([like] * len(_SEARCH_FIELDS))
    where = " AND ".join(clauses)
    offset = max(page - 1, 0) * page_size
    with closing(_connect(db_path)) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM skill_application_forms WHERE {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM skill_application_forms WHERE {where}"
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        ).fetchall()
    return [_row_to_application(r) for r in rows], int(total)


def list_pending_applications_including_hidden(
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List all pending applications including soft-deleted (hidden=1) ones.

    Used for syncing with upstream so that if admin approves/rejects after
    user deletion, the status and hidden flag are updated correctly.
    """
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_application_forms"
            " WHERE status = ? AND hidden IN (0, 1)"
            " ORDER BY updated_at DESC LIMIT 50",
            (PublishStatus.PENDING,),
        ).fetchall()
    return [_row_to_application(r) for r in rows]


def stats_for(
    *, db_path: Path | str | None = None
) -> dict[str, int]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM skill_application_forms"
            " WHERE hidden = 0 GROUP BY status",
        ).fetchall()
    by_status = {PublishStatus.DRAFT: 0, PublishStatus.PENDING: 0,
                 PublishStatus.APPROVED: 0, PublishStatus.REJECTED: 0}
    for row in rows:
        if row["status"] in by_status:
            by_status[row["status"]] = int(row["n"])
    return {"total": sum(by_status.values()), "by_status": by_status}


def has_active_application(
    account: str,
    skill_name: str,
    application_type: str,
    *,
    exclude_id: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Same (account, skill_name, application_type) has a draft/pending hidden=0 form (9.1)."""
    clauses = [
        "submitter_account = ?", "skill_name = ?", "application_type = ?",
        "hidden = 0", f"status IN ('{PublishStatus.DRAFT}', '{PublishStatus.PENDING}')",
    ]
    params: list[Any] = [account, skill_name, application_type]
    if exclude_id:
        clauses.append("id != ?")
        params.append(exclude_id)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM skill_application_forms WHERE " + " AND ".join(clauses) + " LIMIT 1",
            params,
        ).fetchone()
    return row is not None


def has_pending_application(
    skill_name: str,
    *,
    account: str | None = None,
    exclude_id: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Any pending (in-review) form for the skill - submit concurrency guard (7.5 step 2)."""
    clauses = ["skill_name = ?", "hidden = 0", f"status = '{PublishStatus.PENDING}'"]
    params: list[Any] = [skill_name]
    if account:
        clauses.append("submitter_account = ?")
        params.append(account)
    if exclude_id:
        clauses.append("id != ?")
        params.append(exclude_id)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM skill_application_forms WHERE " + " AND ".join(clauses) + " LIMIT 1",
            params,
        ).fetchone()
    return row is not None


def soft_hide(
    app_id: str, *, db_path: Path | str | None = None
) -> bool:
    return update_application(
        app_id, fields={"hidden": 1, "hidden_at": now()}, db_path=db_path
    )


def hard_delete(
    app_id: str, *, db_path: Path | str | None = None
) -> bool:
    """Physically remove the form and its audit log in one transaction (7.9)."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "DELETE FROM skill_application_forms WHERE id = ?", (app_id,)
        )
        conn.execute(
            "DELETE FROM skill_publish_audit_log WHERE application_id = ?", (app_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def insert_audit_log(
    *,
    application_id: str,
    action: str,
    from_status: str,
    to_status: str,
    operator: str,
    operator_role: str,
    comment: str = "",
    operator_name: str = "",
    upstream_application_id: str = "",
    upstream_event_id: int | None = None,
    created_at: float | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """INSERT-only audit log; UNIQUE(upstream_event_id) dedupes replayed events.

    Returns the new row id, or None when the same upstream_event_id already
    exists (idempotent skip).
    """
    try:
        with closing(_connect(db_path)) as conn:
            cur = conn.execute(
                """
                INSERT INTO skill_publish_audit_log (
                  application_id, action, from_status, to_status, operator,
                  operator_name, operator_role, comment, upstream_application_id,
                  upstream_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id, action, from_status, to_status, operator,
                    operator_name, operator_role, comment, upstream_application_id,
                    upstream_event_id, created_at if created_at is not None else now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def list_audit_log(
    application_id: str, *, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_publish_audit_log WHERE application_id = ?"
            " ORDER BY created_at DESC, id DESC",
            (application_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_version(
    skill_name: str, *, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM skill_versions WHERE skill_name = ? AND is_latest = 1",
            (skill_name,),
        ).fetchone()
    return dict(row) if row else None


def insert_version_and_demote(
    *, fields: dict[str, Any], db_path: Path | str | None = None
) -> None:
    """Insert a new latest version and demote the previous one atomically (6.3)."""
    ts = now()
    data = {
        "skill_name": fields["skill_name"],
        "version": fields["version"],
        "submitter_account": fields.get("submitter_account", ""),
        "display_name": fields.get("display_name", ""),
        "upstream_skill_id": fields.get("upstream_skill_id", ""),
        "upstream_status": fields.get("upstream_status", ""),
        "upstream_status_updated_at": ts,
        "application_id": fields.get("application_id", ""),
        "release_notes": fields.get("release_notes", ""),
        "is_latest": 1,
        "released_at": fields.get("released_at", ts),
        "created_at": ts,
        "updated_at": ts,
    }
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE skill_versions SET is_latest = 0, updated_at = ?"
            " WHERE skill_name = ? AND is_latest = 1",
            (ts, data["skill_name"]),
        )
        conn.execute(
            """
            INSERT INTO skill_versions (
              skill_name, version, submitter_account, display_name,
              upstream_skill_id, upstream_status, upstream_status_updated_at,
              application_id, release_notes, is_latest, released_at,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(data.values()),
        )
        conn.commit()


def update_version_status(
    skill_name: str,
    upstream_status: str,
    *,
    db_path: Path | str | None = None,
) -> bool:
    """Scene=3/4 admin on/offline events update the latest version's status (10.8)."""
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            "UPDATE skill_versions SET upstream_status = ?,"
            " upstream_status_updated_at = ?, updated_at = ?"
            " WHERE skill_name = ? AND is_latest = 1",
            (upstream_status, now(), now(), skill_name),
        )
        conn.commit()
        return cur.rowcount > 0


def list_published_skills(
    *, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_versions WHERE upstream_status = '2'"
            " ORDER BY released_at DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def list_application_versions(
    skill_name: str, *, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM skill_application_forms WHERE skill_name = ?"
            " AND submitted_at IS NOT NULL ORDER BY submitted_at ASC",
            (skill_name,),
        ).fetchall()
    return [_row_to_application(r) for r in rows]


def compute_is_first_for_apps(
    apps: list[dict[str, Any]], *, db_path: Path | str | None = None
) -> dict[str, bool]:
    """Return {app_id: is_first} for publish apps in the list.

    A publish application is "first" if no earlier version of the skill has
    been published (no prior row in skill_versions). Unpublish apps default
    to False.
    """
    from integration.skill_publish.version_utils import semver_gt

    result: dict[str, bool] = {}
    publish_skills: list[tuple[str, str]] = []
    for app in apps:
        app_id = app.get("id") or ""
        if app.get("application_type") != "publish":
            result[app_id] = False
            continue
        publish_skills.append((app.get("skill_name") or "", app.get("version") or ""))

    if not publish_skills:
        return result

    skill_names = list({s for s, _ in publish_skills})
    placeholders = ",".join("?" for _ in skill_names)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT skill_name, version FROM skill_versions"
            f" WHERE skill_name IN ({placeholders})",
            skill_names,
        ).fetchall()

    versions_by_skill: dict[str, list[str]] = {}
    for row in rows:
        versions_by_skill.setdefault(row["skill_name"], []).append(row["version"])

    for app in apps:
        app_id = app.get("id") or ""
        if app.get("application_type") != "publish":
            continue
        skill_name = app.get("skill_name") or ""
        app_version = app.get("version") or ""
        prior_versions = versions_by_skill.get(skill_name, [])
        has_older = any(
            semver_gt(app_version, v) for v in prior_versions if v != app_version
        )
        result[app_id] = not has_older
    return result
