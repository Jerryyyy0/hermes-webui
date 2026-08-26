"""Explicit, environment-independent repair operations for manifest records."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def rebind_manifest_turn_records(
    *,
    lineage_key: str,
    profile: str,
    old_key: str,
    new_key: str,
    apply: bool = False,
    db_path: Path | str,
) -> dict[str, Any]:
    """Inspect or atomically apply one explicit historical turn mapping."""
    lineage = str(lineage_key or "").strip()
    normalized_profile = str(profile or "").strip()
    old_turn = str(old_key or "").strip()
    new_turn = str(new_key or "").strip()
    if not lineage or not old_turn or not new_turn:
        raise ValueError("lineage_key, old_key, and new_key are required")
    if old_turn == new_turn:
        raise ValueError("old_key and new_key must differ")

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"manifest database not found: {path}")
    with closing(sqlite3.connect(str(path))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT session_id, lineage_key, profile, turn_key, record_kind,
                   path, preview, source_tool, created_at, updated_at
            FROM session_manifest_records
            WHERE lineage_key = ? AND profile = ? AND turn_key = ?
            ORDER BY record_kind, path
            """,
            (lineage, normalized_profile, old_turn),
        ).fetchall()
        records = [dict(row) for row in rows]
        report: dict[str, Any] = {
            "status": "ready" if records else "not_found",
            "dry_run": not apply,
            "lineage_key": lineage,
            "profile": normalized_profile,
            "old_key": old_turn,
            "new_key": new_turn,
            "record_count": len(records),
            "records": records,
        }
        if not apply or not records:
            return report

        now = time.time()
        with conn:
            for row in records:
                conn.execute(
                    """
                    INSERT INTO session_manifest_records (
                      session_id, lineage_key, profile, turn_key, record_kind,
                      path, preview, source_tool, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lineage_key, profile, turn_key, record_kind, path)
                    DO UPDATE SET
                      session_id = excluded.session_id,
                      preview = excluded.preview,
                      source_tool = excluded.source_tool,
                      updated_at = excluded.updated_at
                    """,
                    (
                        row["session_id"], lineage, normalized_profile, new_turn,
                        row["record_kind"], row["path"], row["preview"], row["source_tool"],
                        row["created_at"], now,
                    ),
                )
            conn.execute(
                """
                DELETE FROM session_manifest_records
                WHERE lineage_key = ? AND profile = ? AND turn_key = ?
                """,
                (lineage, normalized_profile, old_turn),
            )
        report["status"] = "applied"
        report["dry_run"] = False
        return report
