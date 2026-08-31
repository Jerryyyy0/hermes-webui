"""Explicit, environment-independent repair operations for manifest records."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def _dedupe_artifact_entries(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep one file row per canonical path, favouring stronger provenance."""
    from integration.session_manifest.manifest import _artifact_source_priority

    selected: dict[str, dict[str, str]] = {}
    for entry in entries:
        path = str(entry.get('path') or '').strip()
        if not path:
            continue
        current = selected.get(path)
        if current is None or _artifact_source_priority(str(entry.get('source_tool') or '')) > _artifact_source_priority(
            str(current.get('source_tool') or '')
        ):
            selected[path] = {
                'path': path,
                'preview': str(entry.get('preview') or 'file').strip() or 'file',
                'source_tool': str(entry.get('source_tool') or 'assistant_prose').strip() or 'assistant_prose',
            }
    return [selected[path] for path in sorted(selected)]


def _target_turn(session, turn_key: str) -> dict[str, Any] | None:
    from integration.session_manifest.manifest import _message_turns

    key = str(turn_key or '').strip()
    for turn in _message_turns(list(getattr(session, 'messages', None) or [])):
        if str(turn.get('turn_key') or '').strip() == key:
            return turn
    return None


def _file_entries_from_tool_evidence(session, turn: dict[str, Any], workspace: Path) -> list[dict[str, str]]:
    """Return only successful mutation/static-output evidence for one real turn."""
    from integration.session_manifest.manifest import (
        ARTIFACT_MUTATION_TOOLS,
        EXECUTION_ARTIFACT_TOOLS,
        _collect_tool_events,
        _collect_turn_artifact_entries_from_events,
        _tool_calls_for_turn,
    )

    messages = list(getattr(session, 'messages', None) or [])
    start = int(turn['start_msg_idx'])
    end = int(turn['end_msg_idx'])
    scoped_messages = messages[start:end + 1]
    scoped_calls = _tool_calls_for_turn(
        list(getattr(session, 'tool_calls', None) or []),
        start_msg_idx=start,
        end_msg_idx=end,
    )
    events = _collect_tool_events(scoped_messages, scoped_calls)
    allowed = ARTIFACT_MUTATION_TOOLS | EXECUTION_ARTIFACT_TOOLS
    return [
        entry
        for entry in _collect_turn_artifact_entries_from_events(events, workspace)
        if str(entry.get('preview') or 'file') == 'file'
        and str(entry.get('source_tool') or '').strip() in allowed
    ]


def _final_media_entries(session, turn: dict[str, Any], workspace: Path) -> list[dict[str, str]]:
    """Use MEDIA tokens from the turn's final real assistant message only."""
    from integration.session_manifest.manifest import (
        MEDIA_ARTIFACT_SOURCE,
        _is_synthetic_control_message,
        _message_text,
        _paths_from_assistant_media,
    )

    messages = list(getattr(session, 'messages', None) or [])
    start = int(turn['start_msg_idx'])
    end = int(turn['end_msg_idx'])
    for message in reversed(messages[start:end + 1]):
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        if _is_synthetic_control_message(message):
            continue
        return [
            {'path': path, 'preview': 'file', 'source_tool': MEDIA_ARTIFACT_SOURCE}
            for path in _paths_from_assistant_media(_message_text(message.get('content')), workspace)
        ]
    return []


def _later_modified_paths(session, turn: dict[str, Any], workspace: Path) -> set[str]:
    """Find real later turns that successfully mutate an artifact path."""
    from integration.session_manifest.manifest import _message_turns

    turns = _message_turns(list(getattr(session, 'messages', None) or []))
    target_key = str(turn.get('turn_key') or '').strip()
    later = False
    paths: set[str] = set()
    for candidate in turns:
        if str(candidate.get('turn_key') or '').strip() == target_key:
            later = True
            continue
        if not later:
            continue
        paths.update(
            str(entry.get('path') or '').strip()
            for entry in _file_entries_from_tool_evidence(session, candidate, workspace)
            if str(entry.get('path') or '').strip()
        )
    return paths


def _append_missing_records_atomically(
    session,
    turn_key: str,
    rows: list[dict[str, str]],
    *,
    db_path: Path | str | None,
) -> list[dict[str, str]] | None:
    """Append rows and verify their complete identity before committing."""
    from integration.session_manifest import store

    records = [
        record
        for row in rows
        for record in [store._normalize_record(session, turn_key, row, store.ARTIFACT_RECORD_KIND)]
        if record
    ]
    if not records:
        return []
    expected_paths = {str(record['path']) for record in records}
    lineage = str(records[0]['lineage_key'])
    profile = str(records[0]['profile'])
    root = store._effective_workspace_root_text(records[0]['workspace_root'])
    if not root:
        return None
    now = time.time()
    try:
        with closing(store._connect(db_path)) as conn:
            with conn:
                existing_rows = conn.execute(
                    """
                    SELECT path, workspace_root
                    FROM session_manifest_records
                    WHERE lineage_key = ? AND profile = ? AND turn_key = ? AND record_kind = ?
                    """,
                    (lineage, profile, turn_key, store.ARTIFACT_RECORD_KIND),
                ).fetchall()
                persisted_paths = {
                    str(row['path'] or '').strip()
                    for row in existing_rows
                    if store._effective_workspace_root_text(row['workspace_root']) == root
                }
                missing = [record for record in records if str(record['path']) not in persisted_paths]
                for record in missing:
                    conn.execute(
                        """
                        INSERT INTO session_manifest_records (
                          session_id, lineage_key, profile, workspace_root, turn_key, record_kind,
                          path, preview, source_tool, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(lineage_key, profile, turn_key, record_kind, path, workspace_root)
                        DO NOTHING
                        """,
                        (
                            record['session_id'], record['lineage_key'], record['profile'], record['workspace_root'],
                            record['turn_key'], record['record_kind'], record['path'], record['preview'],
                            record['source_tool'], now, now,
                        ),
                    )
                verified_rows = conn.execute(
                    """
                    SELECT path, workspace_root
                    FROM session_manifest_records
                    WHERE lineage_key = ? AND profile = ? AND turn_key = ? AND record_kind = ?
                    """,
                    (lineage, profile, turn_key, store.ARTIFACT_RECORD_KIND),
                ).fetchall()
                verified_paths = {
                    str(row['path'] or '').strip()
                    for row in verified_rows
                    if store._effective_workspace_root_text(row['workspace_root']) == root
                }
                if not expected_paths.issubset(verified_paths):
                    raise sqlite3.IntegrityError('manifest record verification failed')
    except (sqlite3.Error, OSError, ValueError):
        return None
    return [
        {'path': str(record['path']), 'preview': str(record['preview']), 'source_tool': str(record['source_tool'])}
        for record in missing
    ]


def repair_incomplete_manifest_turn(
    session,
    turn_key: str,
    *,
    apply: bool = False,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Safely append strong-evidence artifacts missing from one settled turn.

    This maintenance-only operation never infers a turn, replaces a decision,
    or writes on uncertainty.  It is deliberately separate from manifest GET.
    """
    from integration.session_manifest.manifest import (
        artifact_workspace_root_for_session,
        filter_existing_turn_artifact_entries,
    )
    from integration.session_manifest.store import load_manifest_records, resolve_manifest_lineage_key

    key = str(turn_key or '').strip()
    report: dict[str, Any] = {
        'session_id': str(getattr(session, 'session_id', '') or '').strip(),
        'turn_key': key,
        'dry_run': not apply,
        'added_records': [],
        'unchanged_paths': [],
        'rejected_paths': [],
    }
    if not key:
        return {**report, 'status': 'invalid_turn_key'}
    if bool(getattr(session, '_messages_truncated', False)):
        return {**report, 'status': 'transcript_incomplete'}
    turn = _target_turn(session, key)
    if turn is None:
        return {**report, 'status': 'turn_not_found'}
    try:
        workspace = artifact_workspace_root_for_session(session)
    except (OSError, RuntimeError, ValueError):
        return {**report, 'status': 'workspace_unavailable'}
    root = str(workspace.resolve())
    report.update({
        'lineage_key': resolve_manifest_lineage_key(session),
        'profile': str(getattr(session, 'profile', '') or '').strip(),
        'workspace_root': root,
    })

    candidates = _dedupe_artifact_entries(
        _file_entries_from_tool_evidence(session, turn, workspace)
        + _final_media_entries(session, turn, workspace)
    )
    candidates = filter_existing_turn_artifact_entries(workspace, None, candidates)
    later_paths = _later_modified_paths(session, turn, workspace)
    conflicts = sorted({entry['path'] for entry in candidates if entry['path'] in later_paths})
    if conflicts:
        return {
            **report,
            'status': 'version_conflict',
            'conflicting_paths': conflicts,
            'rejected_paths': conflicts,
        }

    persisted_paths = {
        str(row.get('path') or '').strip()
        for row in load_manifest_records(session, db_path=db_path)
        if str(row.get('turn_key') or '').strip() == key
        and str(row.get('workspace_root') or '').strip() == root
    }
    missing = [entry for entry in candidates if entry['path'] not in persisted_paths]
    report['unchanged_paths'] = sorted(entry['path'] for entry in candidates if entry['path'] in persisted_paths)
    report['missing_records'] = missing
    if not missing:
        return {**report, 'status': 'no_change'}
    if not apply:
        return {**report, 'status': 'dry_run'}
    added = _append_missing_records_atomically(session, key, missing, db_path=db_path)
    if added is None:
        return {**report, 'status': 'store_failed'}
    if not added:
        return {**report, 'status': 'no_change', 'dry_run': False}
    try:
        from integration.session_manifest.manifest import build_session_manifest

        manifest = build_session_manifest(session)
        verified_paths = {
            str(row.get('path') or '').strip()
            for turn_row in manifest.get('turns', [])
            if isinstance(turn_row, dict) and str(turn_row.get('turn_key') or '').strip() == key
            for row in turn_row.get('artifacts', [])
            if isinstance(row, dict)
        }
        if not {entry['path'] for entry in added}.issubset(verified_paths):
            raise RuntimeError('manifest read verification did not return every appended record')
    except Exception:
        return {
            **report,
            'status': 'applied_read_verification_failed',
            'dry_run': False,
            'added_records': added,
        }
    return {**report, 'status': 'applied', 'dry_run': False, 'added_records': added}


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
