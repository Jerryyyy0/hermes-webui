"""Import cron agent sessions into WebUI sidecar (sidebar list excludes cron runs by default)."""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from integration.config import cron_all_profiles_enabled
from integration.crons.listing import (
    _normalize_profile_name,
    _profile_home_for_name,
    resolve_owner_profile_for_job,
)
from integration.project_logging import get_logger
from integration.crons.execution_model import read_profile_default_binding

logger = get_logger(__name__)

# When state.db no longer has the target cron session row, fall back to matching
# owner output .md by the timestamp embedded in cron_<job>_YYYYMMDD_HHMMSS.
CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS = 600.0
CRON_EXECUTION_ARTIFACT_MAX_DELTA_SECONDS = 600.0

_CRON_RUN_HISTORY_FIELDS = (
    "id",
    "title",
    "started_at",
    "ended_at",
    "end_reason",
    "model",
    "message_count",
    "tool_call_count",
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
)

_CRON_OUTPUT_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})\.md$"
)


def _parse_cron_execution_timestamp(value: Any) -> float | None:
    """Parse Hermes execution timestamps without treating malformed data as now."""
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    result = parsed.timestamp()
    return result if math.isfinite(result) else None


def list_cron_job_execution_results(
    execution_home: Path,
    job_id: str,
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read the Agent-owned terminal execution history for one cron job.

    ``cron.executions`` resolves its database from process-global ``HERMES_HOME``.
    History requests may target a different profile, so use the same SQLite file
    through a read-only query instead of importing it under the wrong home.
    """
    job_id = str(job_id or "").strip()
    db_path = Path(execution_home) / "cron" / "executions.db"
    if not job_id or not db_path.is_file():
        return []
    try:
        max_rows = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        max_rows = 500
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(executions)")}
            required = {"id", "job_id", "status", "claimed_at", "started_at", "finished_at", "error"}
            if not required.issubset(columns):
                return []
            rows = conn.execute(
                """
                SELECT id, status, claimed_at, started_at, finished_at, error
                FROM executions
                WHERE job_id = ? AND status IN ('completed', 'failed', 'unknown')
                ORDER BY claimed_at DESC, id DESC
                LIMIT ?
                """,
                (job_id, max_rows),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("cron execution history read failed for %s: %s", job_id, exc)
        return []

    results: list[dict[str, Any]] = []
    for row in rows:
        execution = dict(row)
        status = str(execution.get("status") or "").strip().lower()
        ended_at = _parse_cron_execution_timestamp(execution.get("finished_at"))
        started_at = _parse_cron_execution_timestamp(execution.get("started_at"))
        claimed_at = _parse_cron_execution_timestamp(execution.get("claimed_at"))
        if status == "completed":
            end_reason = "cron_complete"
        elif status == "failed":
            end_reason = "cron_error"
        else:
            end_reason = None
        results.append(
            {
                "execution_id": str(execution.get("id") or ""),
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "claimed_at": claimed_at,
                "end_reason": end_reason,
                "error_detail": str(execution.get("error") or "").strip() or None,
            }
        )
    return results


def _cron_execution_match_time(execution: dict[str, Any]) -> float | None:
    for key in ("ended_at", "started_at", "claimed_at"):
        try:
            value = float(execution.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def match_cron_artifacts_to_execution_results(
    artifacts: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> None:
    """Annotate artifacts with one authoritative, terminal execution result.

    Matching every possible pair first, then accepting the smallest deltas,
    prevents two neighboring output files from consuming the same execution.
    An unmatched artifact deliberately has no inferred completion state.
    """
    pairs: list[tuple[float, int, int, str]] = []
    for artifact_index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        try:
            artifact_time = float(artifact.get("modified"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(artifact_time):
            continue
        for execution_index, execution in enumerate(executions):
            if not isinstance(execution, dict):
                continue
            execution_time = _cron_execution_match_time(execution)
            if execution_time is None:
                continue
            delta = abs(artifact_time - execution_time)
            if delta <= CRON_EXECUTION_ARTIFACT_MAX_DELTA_SECONDS:
                pairs.append(
                    (
                        delta,
                        artifact_index,
                        execution_index,
                        str(execution.get("execution_id") or ""),
                    )
                )

    matched_artifacts: set[int] = set()
    matched_executions: set[int] = set()
    for _delta, artifact_index, execution_index, _execution_id in sorted(pairs):
        if artifact_index in matched_artifacts or execution_index in matched_executions:
            continue
        artifact = artifacts[artifact_index]
        execution = executions[execution_index]
        matched_artifacts.add(artifact_index)
        matched_executions.add(execution_index)
        artifact["execution_status"] = execution.get("status")
        artifact["execution_end_reason"] = execution.get("end_reason")
        artifact["execution_error_detail"] = execution.get("error_detail")
        artifact["execution_started_at"] = execution.get("started_at")
        artifact["execution_ended_at"] = execution.get("ended_at")


def match_cron_artifacts_to_job_last_run_result(
    artifacts: list[dict[str, Any]],
    job: dict,
) -> None:
    """Use the job summary only for its exactly matching latest script run.

    WebUI manual runs execute ``run_job`` in a child process and therefore do
    not create an Agent ``executions.db`` row. ``jobs.json`` retains only the
    most recent terminal result, so it is safe evidence for at most one output
    artifact: the one whose modification time matches ``last_run_at``. Older
    output files remain unverified rather than inheriting a newer failure.
    """
    if not isinstance(job, dict) or not job.get("no_agent"):
        return
    status = str(job.get("last_status") or "").strip().lower()
    if status == "ok":
        execution_status = "completed"
        end_reason = "cron_complete"
        error_detail = None
    elif status == "error":
        execution_status = "failed"
        end_reason = "cron_error"
        error_detail = str(job.get("last_error") or "").strip() or None
    else:
        return
    last_run_at = _parse_cron_execution_timestamp(job.get("last_run_at"))
    if last_run_at is None:
        return

    candidates: list[tuple[float, dict[str, Any]]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("execution_status"):
            continue
        try:
            modified_at = float(artifact.get("modified"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(modified_at):
            continue
        delta = abs(modified_at - last_run_at)
        if delta <= CRON_EXECUTION_ARTIFACT_MAX_DELTA_SECONDS:
            candidates.append((delta, artifact))
    if not candidates:
        return

    _, artifact = min(candidates, key=lambda item: item[0])
    artifact["execution_status"] = execution_status
    artifact["execution_end_reason"] = end_reason
    artifact["execution_error_detail"] = error_detail
    artifact["execution_started_at"] = None
    artifact["execution_ended_at"] = last_run_at


def reconcile_no_agent_cron_output_records(
    execution_home: Path,
    job: dict,
    artifacts: list[dict[str, Any]],
    database_runs: list[dict[str, Any]],
) -> None:
    """Correct prior synthetic no-agent records only when a failure is verified."""
    if not (job or {}).get("no_agent"):
        return
    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id:
        return
    failures = {
        _cron_backfill_session_id(job_id, str(artifact.get("filename") or ""))
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("execution_end_reason") == "cron_error"
    }
    failures.discard(None)
    if not failures:
        return
    corrected = 0
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
            if not {"id", "source", "end_reason"}.issubset(columns):
                return
            for session_id in failures:
                cursor = conn.execute(
                    "UPDATE sessions SET end_reason = 'cron_error' "
                    "WHERE id = ? AND source = 'cron' "
                    "AND (end_reason IS NULL OR end_reason != 'cron_error')",
                    (session_id,),
                )
                corrected += cursor.rowcount
            conn.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning(
            "cron output failure reconciliation failed job_id=%s db_path=%s",
            job_id,
            db_path,
            exc_info=True,
        )
        return

    if corrected:
        session_ids = sorted(str(session_id) for session_id in failures)
        logged_session_ids = session_ids[:10]
        if len(session_ids) > len(logged_session_ids):
            logged_session_ids.append(f"... ({len(session_ids) - len(logged_session_ids)} more)")
        logger.info(
            "cron output failure reconciled job_id=%s db_path=%s corrected=%d session_ids=%s",
            job_id,
            db_path,
            corrected,
            logged_session_ids,
        )

    for run in database_runs:
        if str(run.get("session_id") or "") in failures:
            run["end_reason"] = "cron_error"


def list_cron_job_runs_from_state_db(
    execution_home: Path,
    job_id: str,
) -> list[dict[str, Any]]:
    """Return one job's cron sessions from its execution state database.

    The half-open id range mirrors Hermes Agent's ``list_cron_job_runs`` query,
    keeping work scoped to one job instead of scanning every cron session.
    Older databases are supported by selecting only columns they contain.
    """
    job_id = str(job_id or "").strip()
    db_path = Path(execution_home) / "state.db"
    if not job_id or not db_path.is_file():
        return []

    prefix = f"cron_{job_id}_"
    prefix_hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            session_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if not {"id", "source"}.issubset(session_columns):
                return []
            selected = [field for field in _CRON_RUN_HISTORY_FIELDS if field in session_columns]
            if "id" not in selected:
                selected.insert(0, "id")
            order = "started_at DESC, id DESC" if "started_at" in session_columns else "id DESC"
            rows = conn.execute(
                f"""
                SELECT {', '.join(selected)}
                FROM sessions
                WHERE source = 'cron' AND id >= ? AND id < ?
                ORDER BY {order}
                """,
                (prefix, prefix_hi),
            ).fetchall()

            runs: list[dict[str, Any]] = []
            for row in rows:
                run = dict(row)
                run["session_id"] = run.pop("id")
                # A cron run can later become a normal, growing conversation under
                # the same ID. History must therefore expose only fields persisted
                # on its original sessions row, never values derived from messages.
                run["preview"] = ""
                run["last_active"] = None
                runs.append(run)
            return runs
    except (OSError, sqlite3.Error) as exc:
        logger.debug("cron run history state.db read failed for %s: %s", job_id, exc)
        return []


def _cron_backfill_session_id(job_id: str, filename: str) -> str | None:
    """Derive a stable Agent-compatible cron session ID from an output filename."""
    match = _CRON_OUTPUT_FILENAME_RE.fullmatch(str(filename or ""))
    if not match:
        return None
    try:
        run_at = dt.datetime.strptime(
            f"{match.group('date')} {match.group('time')}",
            "%Y-%m-%d %H-%M-%S",
        )
    except ValueError:
        return None
    return f"cron_{job_id}_{run_at.strftime('%Y%m%d_%H%M%S')}"


def _cron_output_is_already_represented(
    artifact: dict[str, Any],
    database_runs: list[dict[str, Any]],
) -> bool:
    """Keep an output-only artifact from becoming a duplicate synthetic run."""
    try:
        artifact_time = float(artifact.get("modified"))
    except (TypeError, ValueError):
        return False
    for run in database_runs:
        if not isinstance(run, dict) or not run.get("session_id"):
            continue
        try:
            run_time = float(run.get("ended_at") or run.get("started_at"))
        except (TypeError, ValueError):
            continue
        if abs(run_time - artifact_time) <= CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS:
            return True
    return False


def _build_cron_output_run_record(
    job: dict,
    *,
    filename: str,
    output: str,
    ended_at: float | int,
    end_reason: str | None = None,
    execution_error_detail: str | None = None,
    started_at: float | int | None = None,
) -> dict[str, Any] | None:
    """Build the minimal durable record for one cron output artifact."""
    job_id = str((job or {}).get("id") or "").strip()
    session_id = _cron_backfill_session_id(job_id, filename)
    try:
        completed_at = float(ended_at)
    except (TypeError, ValueError):
        return None
    if not job_id or not session_id or not math.isfinite(completed_at) or not isinstance(output, str):
        return None
    try:
        verified_started_at = float(started_at) if started_at is not None else None
    except (TypeError, ValueError):
        verified_started_at = None
    if verified_started_at is not None and not math.isfinite(verified_started_at):
        verified_started_at = None

    resolved_end_reason = str(end_reason or "").strip() or None
    if resolved_end_reason is None:
        if _cron_failure_detail(output, end_reason=None):
            resolved_end_reason = "cron_error"
        elif not ((job or {}).get("no_agent") and not output.strip()):
            resolved_end_reason = "cron_complete"
    job_name = str((job or {}).get("name") or f"Cron {job_id}").strip()
    filename_match = _CRON_OUTPUT_FILENAME_RE.fullmatch(filename)
    try:
        run_label = dt.datetime.strptime(
            f"{filename_match.group('date')} {filename_match.group('time')}",
            "%Y-%m-%d %H-%M-%S",
        ).strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        run_label = dt.datetime.fromtimestamp(completed_at).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": session_id,
        "source": "cron",
        # Hermes Agent enforces a global unique index on non-null titles. Keep
        # the synthesized Agent record unique while the WebUI sidecar retains
        # the user-facing task name.
        "title": f"{job_name} · {run_label}",
        "started_at": verified_started_at if verified_started_at is not None else max(0.0, completed_at - 1.0),
        "ended_at": completed_at,
        "end_reason": resolved_end_reason,
        "messages": build_cron_fallback_messages(
            job,
            output,
            run_mtime=completed_at,
            execution_error_detail=execution_error_detail,
            end_reason=resolved_end_reason,
        ),
    }


def _build_cron_runtime_result_record(
    job: dict,
    *,
    session_id: str,
    ended_at: float | int,
    end_reason: str | None,
    execution_error_detail: str | None,
) -> dict[str, Any] | None:
    """Build a no-agent record when execution finishes before output persists."""
    job_id = str((job or {}).get("id") or "").strip()
    session_id = str(session_id or "").strip()
    if not job_id or not session_id.startswith(f"cron_{job_id}_"):
        return None
    try:
        completed_at = float(ended_at)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(completed_at):
        return None
    job_name = str((job or {}).get("name") or f"Cron {job_id}").strip()
    run_label = dt.datetime.fromtimestamp(completed_at).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": session_id,
        "source": "cron",
        "title": f"{job_name} · {run_label}",
        "started_at": max(0.0, completed_at - 1.0),
        "ended_at": completed_at,
        "end_reason": str(end_reason or "").strip() or None,
        "messages": build_cron_fallback_messages(
            job,
            "",
            run_mtime=completed_at,
            execution_error_detail=execution_error_detail,
            end_reason=end_reason,
        ),
    }


def _import_cron_output_run_records(
    db_path: Path,
    candidates: list[dict[str, Any]],
    *,
    job_id: str,
) -> dict[str, int]:
    """Persist synthesized cron records through Hermes Agent's public store."""
    if not candidates:
        return {"imported": 0, "skipped": 0}
    session_ids = [str(candidate.get("id") or "") for candidate in candidates]
    logged_session_ids = session_ids[:10]
    if len(session_ids) > len(logged_session_ids):
        logged_session_ids.append(f"... ({len(session_ids) - len(logged_session_ids)} more)")
    try:
        from hermes_state import SessionDB

        store = SessionDB(db_path=db_path)
        try:
            result = store.import_sessions(candidates)
        finally:
            store.close()
    except Exception:
        logger.warning(
            "cron output backfill failed job_id=%s db_path=%s session_ids=%s",
            job_id,
            db_path,
            logged_session_ids,
            exc_info=True,
        )
        return {"imported": 0, "skipped": len(candidates)}

    if not isinstance(result, dict) or not result.get("ok"):
        logger.warning(
            "cron output backfill rejected job_id=%s db_path=%s session_ids=%s result=%r",
            job_id,
            db_path,
            logged_session_ids,
            result,
        )
        return {"imported": 0, "skipped": len(candidates)}
    outcome = {
        "imported": int(result.get("imported") or 0),
        "skipped": int(result.get("skipped") or 0),
    }
    if outcome["imported"]:
        logger.info(
            "cron output backfilled job_id=%s db_path=%s imported=%d skipped=%d session_ids=%s",
            job_id,
            db_path,
            outcome["imported"],
            outcome["skipped"],
            logged_session_ids,
        )
    return outcome


def backfill_cron_output_runs_to_state_db(
    job: dict,
    *,
    execution_home: Path,
    artifacts: list[dict[str, Any]],
    database_runs: list[dict[str, Any]],
) -> dict[str, int]:
    """Import legacy output-only cron runs through Hermes Agent's public store.

    A missing state.db record cannot be reconstructed with original tool events
    or exact start timing. We preserve only the job prompt, final output, and
    output-file completion time, and leave every unprovable field absent.
    """
    job_id = str((job or {}).get("id") or "").strip()
    db_path = Path(execution_home) / "state.db"
    if not job_id or not db_path.is_file():
        return {"imported": 0, "skipped": 0}

    candidates: list[dict[str, Any]] = []
    known_session_ids = {
        str(run.get("session_id") or "")
        for run in database_runs
        if isinstance(run, dict) and run.get("session_id")
    }
    for artifact in artifacts:
        if not isinstance(artifact, dict) or _cron_output_is_already_represented(
            artifact, database_runs
        ):
            continue
        session_id = _cron_backfill_session_id(job_id, str(artifact.get("filename") or ""))
        if not session_id or session_id in known_session_ids:
            continue
        output = artifact.get("fallback_output")
        if not isinstance(output, str):
            continue
        execution_end_reason = str(artifact.get("execution_end_reason") or "").strip() or None
        usage = artifact.get("usage")
        model = usage.get("model") if isinstance(usage, dict) else None
        record = _build_cron_output_run_record(
            job,
            filename=str(artifact.get("filename") or ""),
            output=output,
            ended_at=artifact.get("execution_ended_at") or artifact.get("modified"),
            end_reason=execution_end_reason,
            execution_error_detail=artifact.get("execution_error_detail"),
            started_at=artifact.get("execution_started_at"),
        )
        if record is None:
            continue
        record["model"] = str(model or "").strip() or None
        candidates.append(record)

    if not candidates:
        return {"imported": 0, "skipped": 0}

    return _import_cron_output_run_records(db_path, candidates, job_id=job_id)


def cron_sessions_visible_in_sidebar(session: dict) -> bool:
    """Cron sessions are excluded from the default /api/sessions sidebar list."""
    return False


def is_cron_sidebar_session(session: dict) -> bool:
    """Return True for cron execution sessions (not WebUI setup chats)."""
    sid = str(session.get("session_id") or "").strip()
    source = session.get("source_tag") or session.get("source")
    if source == "cron":
        return True
    return sid.startswith("cron_")


def filter_cron_sessions_from_sidebar_rows(rows: list[dict]) -> list[dict]:
    """Drop cron execution rows from GET /api/sessions payloads when integration is on."""
    if not cron_all_profiles_enabled():
        return rows
    return [
        row
        for row in rows
        if isinstance(row, dict) and not is_cron_sidebar_session(row)
    ]


def delete_materialized_cron_session_source(
    sid: str,
    *,
    profile_hint: str | None = None,
) -> dict[str, Any]:
    """Best-effort delete for cron sessions so they don't re-materialize.

    Cron sessions are stored in the Hermes Agent SQLite store (state.db) under the
    *execution* profile home. WebUI's /api/session/delete previously deleted only
    the active profile's state.db row, which can miss cron runs and allow later
    materialization to recreate the WebUI sidecar.
    """
    sid = str(sid or "").strip()
    if not sid or not sid.startswith("cron_"):
        return {"ok": False, "deleted": False, "reason": "not_cron_session"}

    deleted_profiles: list[str] = []
    deleted_output_files: list[str] = []
    attempted_profiles: list[str] = []

    try:
        from api.models import _delete_state_db_session_rows
    except Exception:
        return {"ok": False, "deleted": False, "reason": "missing_state_db_delete_helper"}

    job_id = _cron_job_id_from_session_id(sid)
    if not job_id:
        return {"ok": False, "deleted": False, "reason": "invalid_cron_session_id"}

    owner_profiles = _cron_owner_profiles_for_delete(job_id, sid, profile_hint=profile_hint)
    state_db_profiles = _cron_state_db_profiles_for_delete(job_id, sid, profile_hint=profile_hint)
    state_candidates = _cron_session_candidates_for_profiles(state_db_profiles, job_id)

    for profile_name in owner_profiles:
        attempted_profiles.append(profile_name)
        try:
            owner_home = Path(_profile_home_for_name(profile_name))
            output_file = _delete_cron_output_file_for_run(
                owner_home,
                job_id,
                sid,
                candidates=state_candidates,
            )
            if output_file:
                deleted_output_files.append(str(output_file))
        except Exception:
            continue

    for profile_name in state_db_profiles:
        if profile_name not in attempted_profiles:
            attempted_profiles.append(profile_name)
        try:
            db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
            if _delete_state_db_session_rows(db_path, sid):
                deleted_profiles.append(profile_name)
        except Exception:
            continue

    return {
        "ok": True,
        "deleted": bool(deleted_profiles or deleted_output_files),
        "attempted_profiles": attempted_profiles,
        "deleted_profiles": deleted_profiles,
        "deleted_output_files": deleted_output_files,
    }


def delete_cron_job_history(
    job_id: str,
    *,
    owner_profile: str | None = None,
    job: dict | None = None,
) -> dict[str, Any]:
    """Best-effort cleanup for all history attached to one cron job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "deleted": False, "reason": "missing_job_id"}

    owners = _cron_owner_profiles_for_job_delete(job_id, owner_profile=owner_profile)
    state_db_profiles = _cron_state_db_profiles_for_job_delete(
        job_id,
        owner_profile=owner_profile,
        job=job or {},
    )
    session_ids_by_profile = _cron_session_ids_by_profile(state_db_profiles, job_id)
    session_ids = {
        sid
        for profile_session_ids in session_ids_by_profile.values()
        for sid in profile_session_ids
    }
    session_ids.update(_webui_cron_session_ids_for_job(job_id))

    deleted_sidecars: list[str] = []
    deleted_profiles: list[str] = []
    deleted_output_dirs: list[str] = []
    deleted_output_files = 0
    workspace_cleanup: list[dict[str, Any]] = []
    preserved_sidecars: list[str] = []

    # Sidecars are the recovery owner for V1 roots. Read their immutable
    # binding before deleting anything, and keep a sidecar when cleanup fails
    # so a later explicit retry still has authoritative ownership evidence.
    cleanup_by_session: dict[str, dict[str, Any]] = {}
    try:
        from api.models import Session
        from integration.crons.worktree import cleanup_execution_workspace

        for sid in sorted(session_ids):
            sidecar = Session.load_metadata_only(sid)
            if sidecar is None:
                continue
            if (
                str(getattr(sidecar, "source_tag", "") or "") != "cron"
                or str(getattr(sidecar, "workspace_state", "") or "") != "ready"
                or str(getattr(sidecar, "workspace_mode", "") or "") not in {"managed", "worktree"}
            ):
                continue
            result = cleanup_execution_workspace(
                getattr(sidecar, "workspace", ""),
                session_id=sid,
                mode=str(getattr(sidecar, "workspace_mode", "") or ""),
                repo_root=getattr(sidecar, "worktree_repo_root", None),
            )
            outcome = {"session_id": sid, **result}
            cleanup_by_session[sid] = outcome
            workspace_cleanup.append(outcome)
    except Exception as exc:
        workspace_cleanup.append({"ok": False, "deleted": False, "reason": str(exc)})

    failed_cleanup = [item for item in workspace_cleanup if not item.get("ok")]
    if failed_cleanup:
        # Preserve the sidecar and execution records as the retry handle. The
        # immutable sidecar binding is the only trustworthy ownership evidence.
        try:
            from api.models import Session

            for outcome in failed_cleanup:
                sid = str(outcome.get("session_id") or "").strip()
                session = Session.load(sid) if sid else None
                if session is None or str(getattr(session, "source_tag", "") or "") != "cron":
                    continue
                session.workspace_state = "cleanup_failed"
                session.save()
                preserved_sidecars.append(sid)
        except Exception as exc:
            logger.warning("failed to persist cron cleanup_failed state: %s", exc)
        return {
            "ok": False,
            "deleted": False,
            "job_id": job_id,
            "workspace_cleanup": workspace_cleanup,
            "preserved_sidecars": sorted(set(preserved_sidecars)),
            "reason": "workspace_cleanup_failed",
        }

    try:
        from api.models import _delete_state_db_session_rows_many
    except Exception:
        _delete_state_db_session_rows_many = None

    for sid in sorted(session_ids):
        cleanup = cleanup_by_session.get(sid)
        if cleanup is not None and not cleanup.get("ok"):
            preserved_sidecars.append(sid)
            continue
        if _delete_webui_cron_session_sidecar(sid):
            deleted_sidecars.append(sid)

    if _delete_state_db_session_rows_many is not None:
        for profile_name, profile_session_ids in session_ids_by_profile.items():
            if not profile_session_ids:
                continue
            try:
                db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
                if _delete_state_db_session_rows_many(db_path, profile_session_ids):
                    deleted_profiles.extend(
                        f"{profile_name}:{sid}" for sid in sorted(profile_session_ids)
                    )
            except Exception:
                continue

    for profile_name in owners:
        try:
            output_dir = Path(_profile_home_for_name(profile_name)) / "cron" / "output" / job_id
            removed_count = _delete_cron_output_dir(output_dir)
            if removed_count is not None:
                deleted_output_dirs.append(str(output_dir))
                deleted_output_files += removed_count
        except Exception:
            continue

    deleted = bool(deleted_sidecars or deleted_profiles or deleted_output_dirs)
    if deleted:
        try:
            from api.session_events import publish_session_list_changed

            publish_session_list_changed("cron_job_delete")
        except Exception:
            pass
    return {
        "ok": True,
        "deleted": deleted,
        "job_id": job_id,
        "owner_profiles": owners,
        "state_db_profiles": state_db_profiles,
        "deleted_session_ids": sorted(session_ids),
        "deleted_sidecars": deleted_sidecars,
        "deleted_profiles": deleted_profiles,
        "deleted_output_dirs": deleted_output_dirs,
        "deleted_output_files": deleted_output_files,
        "workspace_cleanup": workspace_cleanup,
        "preserved_sidecars": preserved_sidecars,
    }


def _cron_owner_profiles_for_job_delete(
    job_id: str,
    *,
    owner_profile: str | None = None,
) -> list[str]:
    owners: list[str] = []
    if isinstance(owner_profile, str) and owner_profile.strip():
        owners.append(owner_profile)
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        owners.append(resolved)
    return _dedupe_profile_names(owners)


def _cron_state_db_profiles_for_job_delete(
    job_id: str,
    *,
    owner_profile: str | None = None,
    job: dict | None = None,
) -> list[str]:
    profiles: list[str] = []
    if isinstance(owner_profile, str) and owner_profile.strip():
        profiles.append(owner_profile)
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        profiles.append(resolved)
    execution_profile = _execution_profile_name(job or {})
    if execution_profile:
        profiles.append(execution_profile)
    try:
        from api.profiles import list_profiles_api

        for row in list_profiles_api() or []:
            if isinstance(row, dict) and row.get("name"):
                profiles.append(str(row.get("name")))
    except Exception:
        profiles.append("default")
    return _dedupe_profile_names(profiles)


def _cron_session_ids_for_job(profile_names: list[str], job_id: str) -> set[str]:
    return {
        sid
        for profile_session_ids in _cron_session_ids_by_profile(profile_names, job_id).values()
        for sid in profile_session_ids
    }


def _cron_session_ids_by_profile(
    profile_names: list[str],
    job_id: str,
) -> dict[str, set[str]]:
    session_ids_by_profile: dict[str, set[str]] = {}
    for profile_name in profile_names:
        session_ids: set[str] = set()
        try:
            db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
            if db_path.is_file():
                with closing(sqlite3.connect(str(db_path))) as conn:
                    session_ids = {str(row[0]) for row in _cron_session_candidates(conn, job_id)}
        except (OSError, sqlite3.Error):
            pass
        except Exception:
            pass
        session_ids_by_profile[profile_name] = session_ids
    return session_ids_by_profile


def _webui_cron_session_ids_for_job(job_id: str) -> set[str]:
    try:
        from api.config import SESSION_DIR
    except Exception:
        return set()

    session_ids: set[str] = set()
    try:
        for path in Path(SESSION_DIR).glob("cron_*.json"):
            if path.name.startswith("_"):
                continue
            sid = path.stem
            if _cron_job_id_from_session_id(sid) == job_id:
                session_ids.add(sid)
    except Exception:
        return session_ids
    return session_ids


def materialized_cron_session_ids_for_runs(
    job_id: str,
    runs: list[dict[str, Any]],
    *,
    max_delta_seconds: float = CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS,
) -> dict[str, str]:
    """Map output filenames to already-materialized WebUI cron session IDs.

    This is a fallback for one-shot jobs that were removed from jobs.json before
    history lookup. In that case we cannot call materialize_cron_sessions_for_runs
    with a live job record, but an imported sidecar may already exist.
    """
    sessions: list[tuple[str, float]] = []
    for sid in _webui_cron_session_ids_for_job(job_id):
        ts = _cron_run_timestamp_from_session_id(sid)
        if ts is not None:
            sessions.append((sid, ts))
    if not sessions:
        return {}

    out: dict[str, str] = {}
    for run in runs:
        filename = str(run.get("filename") or "")
        if not filename:
            continue
        try:
            modified = float(run.get("modified") or run.get("run_mtime") or 0)
        except (TypeError, ValueError):
            modified = 0.0
        if modified <= 0:
            continue
        sid, ts = min(sessions, key=lambda item: abs(item[1] - modified))
        if abs(ts - modified) <= max_delta_seconds:
            out[filename] = sid
    return out


def _delete_webui_cron_session_sidecar(sid: str) -> bool:
    try:
        from api.config import LOCK, SESSION_DIR, SESSIONS, _evict_session_agent
        from api.models import is_safe_session_id, prune_session_from_index
    except Exception:
        return False

    sid = str(sid or "").strip()
    if not sid.startswith("cron_") or not is_safe_session_id(sid):
        return False

    deleted = False
    try:
        with LOCK:
            SESSIONS.pop(sid, None)
    except Exception:
        pass
    try:
        _evict_session_agent(sid)
    except Exception:
        pass
    try:
        path = (Path(SESSION_DIR) / f"{sid}.json").resolve()
        path.relative_to(Path(SESSION_DIR).resolve())
        if path.exists():
            deleted = True
        path.unlink(missing_ok=True)
        path.with_suffix(".json.bak").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        prune_session_from_index(sid)
    except Exception:
        pass
    try:
        from api.upload import _session_attachment_dir

        shutil.rmtree(_session_attachment_dir(sid), ignore_errors=True)
    except Exception:
        pass
    return deleted


def _delete_cron_output_dir(output_dir: Path) -> int | None:
    output_dir = Path(output_dir)
    try:
        base = output_dir.parent.resolve()
        target = output_dir.resolve()
        target.relative_to(base)
    except Exception:
        return None
    if not output_dir.exists():
        return None
    if not output_dir.is_dir():
        return None
    try:
        count = len(list(output_dir.glob("*.md")))
        shutil.rmtree(output_dir)
        return count
    except OSError:
        return None


def _cron_session_id_parts(sid: str) -> tuple[str | None, str | None]:
    raw = str(sid or "")
    if not raw.startswith("cron_"):
        return None, None
    body = raw[len("cron_") :]
    # Current shape: cron_<job_id>_YYYYMMDD_HHMMSS. Split the final two
    # timestamp components together so job_id stays ee50... rather than
    # ee50..._YYYYMMDD.
    parts = body.rsplit("_", 3)
    if (
        len(parts) == 4
        and parts[0]
        and len(parts[1]) == 8
        and len(parts[2]) == 6
        and len(parts[3]) == 8
        and parts[1].isdigit()
        and parts[2].isdigit()
        and all(char in "0123456789abcdefABCDEF" for char in parts[3])
    ):
        return parts[0], f"{parts[1]}_{parts[2]}"
    parts = body.rsplit("_", 2)
    if (
        len(parts) == 3
        and parts[0]
        and len(parts[1]) == 8
        and len(parts[2]) == 6
        and parts[1].isdigit()
        and parts[2].isdigit()
    ):
        return parts[0], f"{parts[1]}_{parts[2]}"
    job_id, sep, timestamp = body.rpartition("_")
    if not sep or not job_id:
        return None, None
    return job_id, timestamp


def _cron_job_id_from_session_id(sid: str) -> str | None:
    job_id, _timestamp = _cron_session_id_parts(sid)
    return job_id


def _cron_run_timestamp_from_session_id(sid: str) -> float | None:
    _job_id, ts_raw = _cron_session_id_parts(sid)
    if not ts_raw:
        return None
    # Current Hermes cron ids use cron_<job_id>_YYYYMMDD_HHMMSS. Treat this as
    # local time to match output file mtimes generated by the same process.
    try:
        import datetime as _dt

        parsed = _dt.datetime.strptime(ts_raw, "%Y%m%d_%H%M%S")
        return parsed.timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return float(ts_raw)
    except (TypeError, ValueError):
        return None


def _dedupe_profile_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        normalized = _normalize_profile_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _cron_owner_profiles_for_delete(
    job_id: str,
    sid: str,
    *,
    profile_hint: str | None = None,
) -> list[str]:
    """Profiles whose cron store owns the job output markdown files."""
    owners: list[str] = []
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        owners.append(resolved)
    if isinstance(profile_hint, str) and profile_hint.strip():
        owners.append(profile_hint)
    try:
        from api.models import Session

        meta = Session.load_metadata_only(sid)
        sidecar_profile = getattr(meta, "profile", None) if meta is not None else None
        if isinstance(sidecar_profile, str) and sidecar_profile.strip():
            owners.append(sidecar_profile)
    except Exception:
        pass
    return _dedupe_profile_names(owners)


def _cron_state_db_profiles_for_delete(
    job_id: str,
    sid: str,
    *,
    profile_hint: str | None = None,
) -> list[str]:
    """Profiles that may hold the cron run row in state.db (usually execution home)."""
    profiles: list[str] = []
    if isinstance(profile_hint, str) and profile_hint.strip():
        profiles.append(profile_hint)
    try:
        from api.models import Session

        meta = Session.load_metadata_only(sid)
        sidecar_profile = getattr(meta, "profile", None) if meta is not None else None
        if isinstance(sidecar_profile, str) and sidecar_profile.strip():
            profiles.append(sidecar_profile)
    except Exception:
        pass

    owner = resolve_owner_profile_for_job(job_id)
    if owner:
        profiles.append(owner)
        try:
            from api.profiles import cron_profile_context_for_home
            from cron.jobs import get_job

            with cron_profile_context_for_home(_profile_home_for_name(owner)):
                job = get_job(job_id)
            execution_profile = _execution_profile_name(job or {})
            if execution_profile:
                profiles.append(execution_profile)
        except Exception:
            pass

    try:
        from api.profiles import list_profiles_api

        for row in list_profiles_api() or []:
            if isinstance(row, dict) and row.get("name"):
                profiles.append(str(row.get("name")))
    except Exception:
        profiles.append("default")

    return _dedupe_profile_names(profiles)


def _cron_session_candidates_for_profiles(
    profile_names: list[str],
    job_id: str,
) -> list[tuple[str, str, float | None, str]]:
    candidates_by_id: dict[str, tuple[str, str, float | None, str]] = {}
    for profile_name in profile_names:
        try:
            db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
            if not db_path.is_file():
                continue
            with closing(sqlite3.connect(str(db_path))) as conn:
                for row in _cron_session_candidates(conn, job_id):
                    candidates_by_id.setdefault(row[0], row)
        except sqlite3.Error:
            continue
        except Exception:
            continue
    return sorted(
        candidates_by_id.values(),
        key=lambda row: float(row[2] or 0),
        reverse=True,
    )


def _target_sid_in_candidates(
    candidates: list[tuple[str, str, float | None, str]],
    sid: str,
) -> bool:
    return any(str(row[0]) == sid for row in candidates)


def _find_output_by_timestamp_fallback(
    files: list[Path],
    sid: str,
    *,
    max_delta_seconds: float = CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS,
) -> Path | None:
    """Pick the nearest output .md when state.db no longer has the target session row."""
    parsed_ts = _cron_run_timestamp_from_session_id(sid)
    if parsed_ts is None or not files:
        return None
    try:
        target = float(parsed_ts)
    except (TypeError, ValueError):
        return None

    scored: list[tuple[Path, float]] = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        scored.append((path, abs(mtime - target)))
    if not scored:
        return None

    chosen, delta = min(scored, key=lambda item: item[1])
    if delta > max_delta_seconds:
        logger.debug(
            "cron orphan output skip: sid=%s nearest=%s delta=%.1fs > %.1fs",
            sid,
            chosen.name,
            delta,
            max_delta_seconds,
        )
        return None
    return chosen


def _find_cron_output_file_for_run(
    owner_home: Path,
    job_id: str,
    *,
    sid: str,
    candidates: list[tuple[str, str, float | None, str]],
) -> Path | None:
    """Locate one cron output markdown file using the same mtime heuristic as import."""
    output_dir = Path(owner_home) / "cron" / "output" / job_id
    if not output_dir.is_dir():
        return None
    try:
        files = sorted(output_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not files:
        return None

    if candidates and _target_sid_in_candidates(candidates, sid):
        matches: list[tuple[Path, float]] = []
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            selected = _select_cron_session_candidate(candidates, run_mtime=mtime)
            if selected and selected[0] == sid:
                matches.append((path, mtime))
        if matches:
            target = next((row for row in candidates if row[0] == sid), None)
            try:
                started_at = float(target[2] or 0) if target else 0.0
            except (TypeError, ValueError):
                started_at = 0.0
            chosen, _mtime = min(
                matches,
                key=lambda item: abs(item[1] - started_at) if started_at else item[1],
            )
            return chosen

    # Orphan cleanup: state.db row for this sid is gone (or never visible), but the
    # owner-profile output artifact may still exist and keep /api/crons/history populated.
    return _find_output_by_timestamp_fallback(files, sid)


def _delete_cron_output_file_for_run(
    owner_home: Path,
    job_id: str,
    sid: str,
    *,
    candidates: list[tuple[str, str, float | None, str]] | None = None,
) -> Path | None:
    """Delete the single cron output file owned by the job storage profile."""
    output_dir = Path(owner_home) / "cron" / "output" / job_id
    if not output_dir.is_dir():
        return None

    chosen = _find_cron_output_file_for_run(
        owner_home,
        job_id,
        sid=sid,
        candidates=candidates or [],
    )
    if chosen is None:
        return None

    try:
        chosen.unlink()
        return chosen
    except OSError:
        return None


def _target_profile_for_job(job: dict, owner_profile: str) -> str:
    from api.routes import _available_cron_profile_names, _normalize_cron_profile_value

    raw = str((job or {}).get("profile") or "").strip()
    if raw:
        try:
            normalized = _normalize_cron_profile_value(raw)
            if normalized:
                return normalized
        except ValueError:
            pass
        if raw in _available_cron_profile_names():
            return raw
    return _normalize_profile_name(owner_profile)


def _execution_profile_name(job: dict) -> str | None:
    raw = str((job or {}).get("profile") or "").strip()
    return raw or None


def _cron_session_candidates(conn, job_id: str) -> list[tuple[str, str, float | None, str]]:
    pattern = f"cron_{job_id}_%"
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sessions)")
    cols = {str(row[1]) for row in cur.fetchall()}
    if "source" not in cols:
        return []
    order = "started_at DESC" if "started_at" in cols else "id DESC"
    model_expr = "model" if "model" in cols else "NULL AS model"
    cur.execute(
        f"""
        SELECT id, title, started_at, {model_expr} FROM sessions
        WHERE source = 'cron' AND id LIKE ?
        ORDER BY {order}
        """,
        (pattern,),
    )
    rows = []
    for row in cur.fetchall():
        sid = str(row[0])
        title = str(row[1] or "")
        started = row[2] if len(row) > 2 else None
        model = str(row[3] or "").strip() if len(row) > 3 else ""
        rows.append((sid, title, started, model))
    return rows


def _latest_cron_session_id(conn, job_id: str) -> tuple[str, str, float | None, str] | None:
    candidates = _cron_session_candidates(conn, job_id)
    return candidates[0] if candidates else None


def _select_cron_session_candidate(
    candidates: list[tuple[str, str, float | None, str]],
    *,
    run_mtime: float | None = None,
) -> tuple[str, str, float | None, str] | None:
    if not candidates:
        return None
    if run_mtime is None:
        return candidates[0]

    def _started(row):
        try:
            return float(row[2] or 0)
        except (TypeError, ValueError):
            return 0.0

    # Output files are written after the agent run completes. Prefer the newest
    # cron session that started before that file mtime; fall back to nearest.
    before = [row for row in candidates if _started(row) and _started(row) <= run_mtime + 5]
    if before:
        return max(before, key=_started)
    return min(candidates, key=lambda row: abs((_started(row) or run_mtime) - run_mtime))


def _select_cron_session_for_run(
    conn,
    job_id: str,
    *,
    run_mtime: float | None = None,
    session_id: str | None = None,
) -> tuple[str, str, float | None, str] | None:
    if session_id:
        candidates = _cron_session_candidates(conn, job_id)
        for candidate in candidates:
            if candidate[0] == session_id:
                return candidate
        return None
    candidates = _cron_session_candidates(conn, job_id)
    return _select_cron_session_candidate(candidates, run_mtime=run_mtime)


def _cron_session_completion(conn, sid: str) -> tuple[str | None, float | None]:
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        selected = [field for field in ("end_reason", "ended_at") if field in columns]
        if not selected:
            return None, None
        row = conn.execute(
            f"SELECT {', '.join(selected)} FROM sessions WHERE id = ?",
            (sid,),
        ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    values = dict(zip(selected, row))
    try:
        ended_at = float(values.get("ended_at")) if values.get("ended_at") is not None else None
    except (TypeError, ValueError):
        ended_at = None
    return str(values.get("end_reason") or "").strip() or None, ended_at


def resolve_cron_execution_ended_at(session) -> float | None:
    """Return a verified cron execution boundary for a materialized session.

    A persisted sidecar boundary wins. Legacy sidecars may recover a missing
    boundary only from the matching execution Profile's state.db session row;
    message timestamps and output-file mtimes are not execution boundaries.
    """
    raw_ended_at = getattr(session, "cron_execution_ended_at", None)
    if raw_ended_at not in (None, ""):
        try:
            ended_at = float(raw_ended_at)
        except (TypeError, ValueError):
            return None
        return ended_at if math.isfinite(ended_at) else None

    sid = str(getattr(session, "session_id", "") or "").strip()
    profile = str(
        getattr(session, "cron_execution_profile", None)
        or getattr(session, "profile", None)
        or ""
    ).strip()
    if not sid or not profile:
        return None

    try:
        db_path = Path(_profile_home_for_name(profile)) / "state.db"
    except Exception:
        return None
    if not db_path.is_file():
        return None

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            _end_reason, ended_at = _cron_session_completion(conn, sid)
    except sqlite3.Error as exc:
        logger.debug("cron execution boundary state.db read failed for %s: %s", sid, exc)
        return None
    return ended_at if ended_at is not None and math.isfinite(ended_at) else None


def _cron_output_body(text: str) -> str:
    """Return agent reply body from a cron output markdown file."""
    lines = str(text or "").split("\n")
    response_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("## Response") or line.startswith("# Response"):
            response_idx = i
            break
    if response_idx >= 0:
        return "\n".join(lines[response_idx + 1 :]).strip()
    return "\n".join(lines).strip()


def build_cron_fallback_messages(
    job: dict,
    output_content: str,
    *,
    run_mtime: float | None = None,
    execution_error_detail: str | None = None,
    end_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Synthetic user/assistant pair when state.db has no cron run messages."""
    if (job or {}).get("no_agent"):
        job_name = str((job or {}).get("name") or (job or {}).get("id") or "定时脚本任务")
        prompt = f"定时脚本任务「{job_name}」的本次运行结果如下。"
    else:
        prompt = str((job or {}).get("prompt") or "")
    body = _cron_output_body(output_content)
    if not body:
        detail = str(execution_error_detail or "").strip()
        if detail and (job or {}).get("no_agent"):
            body = f"脚本执行失败。\n\n{detail}"
        elif (job or {}).get("no_agent") and not str(end_reason or "").strip():
            body = "脚本任务未产生输出；本次运行状态尚未验证。"
        else:
            body = "(Cron completed without output)"
    ts = float(run_mtime) if run_mtime is not None else time.time()
    return [
        {
            "role": "user",
            "content": prompt,
            "timestamp": ts - 1,
            "source": "cron_fallback",
        },
        {
            "role": "assistant",
            "content": body,
            "timestamp": ts,
            "source": "cron_fallback",
        },
    ]


_CRON_FAILURE_END_REASONS = frozenset({"cron_failed", "cron_error"})


def _cron_error_timestamp(value: float | int | None) -> float:
    try:
        return float(value) if value is not None else time.time()
    except (TypeError, ValueError):
        return time.time()


def _cron_failure_detail(output_content: str | None, end_reason: str | None) -> str:
    """Extract explicit failure evidence without treating normal output as an error."""
    output = str(output_content or "")
    reason = str(end_reason or "").strip().lower()
    if "(FAILED)" in output and "## Error" in output:
        return output.split("## Error", 1)[1].strip() or reason
    if "**Mode:** no_agent (script)" in output and "**Status:** script failed" in output:
        return output
    if reason in _CRON_FAILURE_END_REASONS:
        return output.strip() or reason
    return ""


def _build_cron_provider_error_message(
    output_content: str | None,
    *,
    end_reason: str | None,
    timestamp: float | int | None,
) -> dict | None:
    """Build a cron failure message using the normal persisted provider-error shape."""
    detail = _cron_failure_detail(output_content, end_reason)
    if not detail:
        return None
    from integration.chat_provider_errors import (
        build_persisted_provider_error_message,
        classify_provider_error,
        provider_error_payload_from_classification,
    )

    classification = classify_provider_error(detail)
    payload = provider_error_payload_from_classification(detail, classification)
    return build_persisted_provider_error_message(
        payload,
        err_type=classification["type"],
        timestamp=_cron_error_timestamp(timestamp),
    )


def _cron_script_failure_detail(output_content: str | None, end_reason: str | None) -> str:
    """Return the script's failure body without cron Markdown framing."""
    detail = _cron_failure_detail(output_content, end_reason)
    marker = "**Status:** script failed"
    if marker in detail:
        return detail.split(marker, 1)[1].strip() or detail
    return detail


def _build_cron_script_error_message(
    output_content: str | None,
    *,
    end_reason: str | None,
    timestamp: float | int | None,
    execution_error_detail: str | None = None,
) -> dict | None:
    """Build a durable error turn for a script-only cron run.

    Script output is not a model/provider failure. In particular, phrases such
    as ``Script not found`` must never be classified as ``model_not_found``.
    """
    detail = str(execution_error_detail or "").strip() or _cron_script_failure_detail(
        output_content,
        end_reason,
    )
    if not detail:
        return None
    return {
        "role": "assistant",
        "content": "**脚本执行失败:** 本次定时脚本未能成功完成。\n\n*请查看脚本错误详情*",
        "timestamp": _cron_error_timestamp(timestamp),
        "_error": True,
        "_error_type": "cron_script_error",
        "provider_details": detail,
        "provider_details_label": "脚本错误详情",
    }


def _build_cron_error_message(
    job: dict,
    output_content: str | None,
    *,
    end_reason: str | None,
    timestamp: float | int | None,
    execution_error_detail: str | None = None,
) -> dict | None:
    if (job or {}).get("no_agent"):
        return _build_cron_script_error_message(
            output_content,
            end_reason=end_reason,
            timestamp=timestamp,
            execution_error_detail=execution_error_detail,
        )
    return _build_cron_provider_error_message(
        output_content,
        end_reason=end_reason,
        timestamp=timestamp,
    )


def _has_matching_cron_error(messages: list[dict] | None, error_message: dict) -> bool:
    """Avoid adding the same durable cron failure during repeated materialization."""
    detail = error_message.get("provider_details")
    error_type = error_message.get("_error_type")
    for message in messages or []:
        if not isinstance(message, dict) or not message.get("_error"):
            continue
        if (
            detail
            and message.get("_error_type") == error_type
            and message.get("provider_details") == detail
        ):
            return True
    return False


def _reconcile_cron_error(
    messages: list[dict],
    error_message: dict | None,
    *,
    legacy_no_agent_detail: str | None = None,
) -> bool:
    """Append a missing cron error or replace a provably misclassified legacy one."""
    if error_message is None:
        return False

    removed_legacy = False
    if error_message.get("_error_type") == "cron_script_error" and legacy_no_agent_detail:
        retained_messages = []
        for message in messages:
            if not isinstance(message, dict) or not message.get("_error"):
                retained_messages.append(message)
                continue
            if (
                message.get("_error_type") != "cron_script_error"
                and message.get("provider_details") == legacy_no_agent_detail
            ):
                removed_legacy = True
                continue
            retained_messages.append(message)
        if removed_legacy:
            messages[:] = retained_messages

    if _has_matching_cron_error(messages, error_message):
        return removed_legacy
    messages.append(error_message)
    return True


def _append_missing_cron_response(
    messages: list[dict],
    output_content: str | None,
    *,
    timestamp: float | int | None,
) -> bool:
    """Restore a missing successful cron final reply without replacing later chat turns."""
    body = _cron_output_body(str(output_content or ""))
    if not body:
        return False
    normalized = " ".join(body.split())
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if " ".join(str(message.get("content") or "").split()) == normalized:
            return False
    messages.append(
        {
            "role": "assistant",
            "content": body,
            "timestamp": _cron_error_timestamp(timestamp),
            "source": "cron_fallback",
        }
    )
    messages.sort(key=lambda message: _cron_error_timestamp(message.get("timestamp")))
    return True


def cron_execution_prefix_and_suffix(
    session,
    messages: list | None = None,
) -> tuple[list, list] | None:
    """Split a growing cron sidecar without reordering later WebUI turns."""
    try:
        ended_at = float(getattr(session, "cron_execution_ended_at", None))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ended_at):
        return None

    source_messages = (
        getattr(session, "messages", None) or []
        if messages is None
        else messages
    )
    messages = list(source_messages)
    split_at = len(messages)
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return None
        try:
            timestamp = float(message.get("timestamp"))
        except (TypeError, ValueError):
            return None
        if timestamp > ended_at:
            split_at = index
            break
    prefix = messages[:split_at]
    suffix = messages[split_at:]
    if any(
        not isinstance(message, dict)
        or _cron_error_timestamp(message.get("timestamp")) <= ended_at
        for message in suffix
    ):
        return None
    return prefix, suffix


def _normalized_cron_fallback_user_content(message: dict) -> str | None:
    """Return the display-equivalent content for one fallback user message."""
    if (
        not isinstance(message, dict)
        or message.get("role") != "user"
        or message.get("source") != "cron_fallback"
    ):
        return None
    from api.streaming import _strip_cron_execution_hint

    return " ".join(_strip_cron_execution_hint(message.get("content") or "").split())


def _consume_matching_cron_fallback_state_user(
    sidecar_messages: list[dict],
    state_messages: list[dict],
) -> list[dict]:
    """Avoid replaying the first real user row over a synthetic cron fallback.

    A cron output file can become visible before the Agent commits its transcript
    to ``state.db``. In that gap the sidecar contains a ``cron_fallback`` user
    with the bare job prompt. The Agent's first persisted user row commonly has
    the scheduler execution hint prepended, which display rendering removes.
    Treat that row as confirmation of the placeholder rather than a second
    visible user turn. Only the first state.db user row is eligible: later,
    identical user content may be a deliberate in-session retry and must remain.
    """
    fallback_users = [
        message
        for message in sidecar_messages
        if _normalized_cron_fallback_user_content(message) is not None
    ]
    if not fallback_users:
        return state_messages

    first_state_user_index = next(
        (
            index
            for index, message in enumerate(state_messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        None,
    )
    if first_state_user_index is None:
        return state_messages

    state_user = state_messages[first_state_user_index]
    from api.streaming import _strip_cron_execution_hint

    normalized_state_content = " ".join(
        _strip_cron_execution_hint(state_user.get("content") or "").split()
    )
    if not normalized_state_content:
        return state_messages
    if normalized_state_content not in {
        normalized
        for fallback_user in fallback_users
        if (normalized := _normalized_cron_fallback_user_content(fallback_user)) is not None
    }:
        return state_messages

    return [
        message
        for index, message in enumerate(state_messages)
        if index != first_state_user_index
    ]


def reconcile_cron_session_transcript(
    session,
    *,
    job: dict | None = None,
    fallback_output: str | None = None,
    run_mtime: float | int | None = None,
) -> bool:
    """Reconcile the Agent-owned cron prefix without touching WebUI follow-ups."""
    if session is None or str(getattr(session, "source_tag", "") or "") != "cron":
        return False
    from api.models import (
        _session_message_dedup_key,
        get_state_db_session_messages,
        merge_session_messages_append_only,
    )

    split = cron_execution_prefix_and_suffix(session)
    if split is None:
        # Legacy materialization may not yet know execution_ended_at. It has no
        # reply suffix to preserve, so retain the historical whole-transcript
        # fallback here. The reply-start prepare gate remains fail-closed.
        sidecar_prefix = list(getattr(session, "messages", None) or [])
        suffix = []
    else:
        sidecar_prefix, suffix = split
    execution_profile = str(
        getattr(session, "cron_execution_profile", None) or getattr(session, "profile", None) or ""
    ).strip() or None
    try:
        execution_ended_at = float(getattr(session, "cron_execution_ended_at", None))
    except (TypeError, ValueError):
        execution_ended_at = None
    all_db_messages = get_state_db_session_messages(session.session_id, profile=execution_profile)
    db_messages = [
        message
        for message in all_db_messages
        if isinstance(message, dict)
        and (
            execution_ended_at is None
            or _cron_error_timestamp(message.get("timestamp")) <= execution_ended_at
        )
    ]
    db_messages = _consume_matching_cron_fallback_state_user(
        sidecar_prefix,
        db_messages,
    )
    agent_snapshot_is_authoritative = bool(
        split is None
        and db_messages
        and not any(
            isinstance(message, dict) and str(message.get("source") or "").strip()
            for message in sidecar_prefix
        )
    )
    if agent_snapshot_is_authoritative:
        # While no terminal boundary exists, WebUI follow-ups are blocked. The
        # active state.db rows are therefore the whole current Agent snapshot;
        # compression may have replaced it and rewritten its timestamps.
        merged_prefix = list(db_messages)
    else:
        merged_prefix = merge_session_messages_append_only(
            sidecar_prefix,
            db_messages,
            truncation_watermark=getattr(session, "truncation_watermark", None),
        )
        known = {
            _session_message_dedup_key(message)
            for message in merged_prefix
            if isinstance(message, dict)
        }
        for message in db_messages:
            key = _session_message_dedup_key(message)
            if key not in known:
                merged_prefix.append(message)
                known.add(key)
    changed = merged_prefix != sidecar_prefix
    if fallback_output and job is not None and not any(
        isinstance(message, dict) and message.get("role") == "user"
        for message in merged_prefix
    ):
        # Output can arrive after an empty sidecar was materialized but before
        # the Agent commits its execution prompt to state.db. Restore the
        # synthetic user anchor so the fallback assistant cannot become the
        # first visible message in the cron session.
        fallback_user = build_cron_fallback_messages(
            job,
            fallback_output,
            run_mtime=run_mtime,
        )[0]
        merged_prefix.append(fallback_user)
        merged_prefix.sort(key=lambda message: _cron_error_timestamp(message.get("timestamp")))
        changed = True
    if fallback_output and not any(
        isinstance(message, dict) and message.get("role") == "assistant"
        for message in db_messages
    ):
        changed = _append_missing_cron_response(
            merged_prefix,
            fallback_output,
            timestamp=run_mtime or getattr(session, "created_at", None),
        ) or changed
    from integration.crons.hooks import (
        _stamp_cron_manifest_turn_keys,
        normalize_cron_manifest_messages,
    )

    merged_prefix = _stamp_cron_manifest_turn_keys(
        normalize_cron_manifest_messages(
            merged_prefix,
            collapse_execution_replayed_users=split is not None,
        )
    )
    changed = merged_prefix != sidecar_prefix
    if changed:
        session.messages = [*merged_prefix, *suffix]
    return changed


def read_cron_output_for_run(
    job_id: str,
    *,
    run_mtime: float | None = None,
    filename: str | None = None,
) -> tuple[str | None, str | None]:
    """Read a cron run .md file for fallback message materialization."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return None, None
    try:
        from cron.jobs import OUTPUT_DIR as CRON_OUT
    except ImportError:
        return None, None

    out_dir = Path(CRON_OUT) / job_id
    if not out_dir.is_dir():
        return None, None

    if filename:
        path = out_dir / filename
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace"), filename
            except OSError:
                return None, None
        return None, None

    files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None, None

    if run_mtime is not None:
        try:
            target = float(run_mtime)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            before = [f for f in files if f.stat().st_mtime <= target + 5]
            if before:
                chosen = max(before, key=lambda f: f.stat().st_mtime)
                try:
                    return chosen.read_text(encoding="utf-8", errors="replace"), chosen.name
                except OSError:
                    return None, None

    try:
        return files[0].read_text(encoding="utf-8", errors="replace"), files[0].name
    except OSError:
        return None, None


def _can_repair_unverified_cron_workspace(session: Any) -> bool:
    """Return whether a fallback sidecar can safely adopt Agent's cwd.

    A Cron execution contributes one real user prompt. A second real user turn
    means the user has already continued the transcript using the fallback
    workspace, so rebinding it would silently split that conversation's
    workspace. Agent-only control rows, including its max-iteration summary
    request, do not count as a user continuation.
    """
    if getattr(session, "source_tag", None) != "cron":
        return False
    if getattr(session, "workspace_state", None) != "workspace_unverified":
        return False
    from integration.crons.hooks import _is_max_iteration_summary_request, _is_real_user_message

    messages = list(getattr(session, "messages", None) or [])
    user_messages = sum(
        1
        for index, message in enumerate(messages)
        if _is_real_user_message(message)
        and not _is_max_iteration_summary_request(messages, index)
    )
    return user_messages <= 1


def _settle_materialized_cron_session(session: Any) -> dict[str, object]:
    """Persist missing Manifest decisions after a Cron sidecar is durable.

    Materialization must remain usable when the execution boundary or manifest
    store is temporarily unavailable, so settlement reports an explicit state
    instead of changing the materialization result or raising into callers.
    """
    from integration.crons.hooks import settle_materialized_cron_session

    session_id = str(getattr(session, "session_id", "") or "?")
    profile = str(getattr(session, "profile", "") or "")
    try:
        settlement = settle_materialized_cron_session(session)
    except Exception as exc:
        logger.warning(
            "cron manifest settlement failed for session_id=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "session_id": session_id,
            "profile": profile,
            "stage": "unexpected",
            "error_type": type(exc).__name__,
        }
    if settlement.status != "persisted":
        logger.warning(
            "cron manifest settlement %s for session_id=%s stage=%s",
            settlement.status,
            session_id,
            settlement.error_stage or "unknown",
        )
        return {
            "status": settlement.status,
            "session_id": session_id,
            "profile": profile,
            "stage": settlement.error_stage or "unknown",
            "settled_turn_keys": settlement.settled_turn_keys,
        }
    return {
        "status": "persisted",
        "session_id": session_id,
        "profile": profile,
        "stage": "complete",
        "next_turn_key": settlement.next_turn_key,
        "settled_turn_keys": settlement.settled_turn_keys,
    }


def _cron_history_sidecar_is_complete(
    sidecar: Any,
    *,
    job: dict,
    target_profile: str,
    execution_profile: str,
    run: dict[str, Any],
) -> bool:
    """Return whether history can safely reuse an existing sidecar.

    History is a read path.  This deliberately proves only persisted metadata
    that is cheap and stable; uncertainty falls back to the existing full
    materialization path, which can reconcile Agent messages and Manifest
    decisions.
    """
    if sidecar is None:
        return False
    if str(getattr(sidecar, "source_tag", "") or "").strip() != "cron":
        return False
    if _normalize_profile_name(getattr(sidecar, "profile", None) or "") != target_profile:
        return False
    if _normalize_profile_name(getattr(sidecar, "cron_execution_profile", None) or "") != execution_profile:
        return False
    if getattr(sidecar, "is_cli_session", True) is not False:
        return False
    if not getattr(sidecar, "project_id", None):
        return False
    model = str(getattr(sidecar, "model", "") or "").strip().lower()
    if not model or model in {"unknown", "none", "null", "n/a"}:
        return False
    if getattr(sidecar, "active_stream_id", None) or getattr(sidecar, "pending_user_message", None):
        return False

    message_count = getattr(sidecar, "_metadata_message_count", None)
    if not isinstance(message_count, int) or message_count <= 0:
        return False

    try:
        boundary = float(getattr(sidecar, "cron_execution_ended_at", None))
        run_ended_at = float((run or {}).get("ended_at"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(boundary) or not math.isfinite(run_ended_at):
        return False
    if abs(boundary - run_ended_at) > 1.0:
        return False

    workspace_state = str(getattr(sidecar, "workspace_state", "") or "").strip()
    if isinstance((job or {}).get("workspace_policy"), dict):
        if workspace_state != "ready":
            return False
    elif workspace_state not in {"ready", "legacy_shared"}:
        return False

    end_reason = str((run or {}).get("end_reason") or "").strip().lower()
    if ("error" in end_reason or "fail" in end_reason) and not getattr(sidecar, "last_error_at", None):
        return False
    return True


def _materialize_and_settle_cron_session_found(
    job: dict,
    found: tuple[str, str, float | None, str],
    **kwargs,
) -> str:
    """Materialize and settle one execution while holding its session lock."""
    from api.config import _get_session_agent_lock
    from api.models import Session

    sid = str(found[0] or "").strip()
    target_profile = str(kwargs.get("target_profile") or "").strip()
    with _get_session_agent_lock(sid):
        materialized_sid = _materialize_cron_session_found(job, found, **kwargs)
        session = Session.load(materialized_sid)
        if session is None:
            logger.warning(
                "cron manifest settlement failed for session_id=%s stage=sidecar_missing",
                sid,
            )
            return materialized_sid
        session_profile = _normalize_profile_name(getattr(session, "profile", None) or "")
        if target_profile and session_profile != _normalize_profile_name(target_profile):
            logger.warning(
                "cron manifest settlement skipped for session_id=%s stage=profile_mismatch",
                sid,
            )
            return materialized_sid
        settlement = _settle_materialized_cron_session(session)
        logger.info(
            "cron manifest settlement result session_id=%s profile=%s status=%s stage=%s settled_turns=%d",
            sid,
            settlement.get("profile", ""),
            settlement.get("status", "unknown"),
            settlement.get("stage", "unknown"),
            len(settlement.get("settled_turn_keys") or ()),
        )
        return materialized_sid


def _materialize_cron_session_found(
    job: dict,
    found: tuple[str, str, float | None, str],
    *,
    target_profile: str,
    execution_profile: str | None,
    fallback_output: str | None = None,
    run_mtime: float | None = None,
    end_reason: str | None = None,
    execution_ended_at: float | None = None,
    execution_error_detail: str | None = None,
    workspace_binding=None,
    state_db_cwd: str | None = None,
) -> str:
    sid, cli_title, started_at, model = found
    model = str(model or "").strip()
    model_provider = None
    if (job or {}).get("no_agent"):
        try:
            profile_home = _profile_home_for_name(target_profile)
            bound_model, model_provider = read_profile_default_binding(profile_home)
            if not model or model.lower() == "unknown":
                model = bound_model
        except Exception:
            logger.warning(
                "cron profile model binding read failed for session_id=%s profile=%s",
                sid,
                target_profile,
                exc_info=True,
            )
    error_timestamp = run_mtime or started_at
    cron_error_message = _build_cron_error_message(
        job,
        fallback_output,
        end_reason=end_reason,
        timestamp=error_timestamp,
        execution_error_detail=execution_error_detail,
    )
    legacy_no_agent_detail = (
        _cron_failure_detail(fallback_output, end_reason)
        if (job or {}).get("no_agent")
        else None
    )

    from api.models import Session, ensure_cron_project, import_cli_session
    from api.models import get_state_db_session_messages
    from api.session_events import publish_session_list_changed

    existing = Session.load_metadata_only(sid)
    if existing is not None:
        existing_profile = getattr(existing, "profile", None) or ""
        if _normalize_profile_name(existing_profile) == target_profile:
            execution_source = execution_profile or target_profile
            needs_update = (
                not getattr(existing, "project_id", None)
                or getattr(existing, "is_cli_session", None) is not False
                or getattr(existing, "source_tag", None) != "cron"
                or getattr(existing, "cron_execution_profile", None) != execution_source
            )
            if workspace_binding is not None:
                needs_update = needs_update or (
                    str(getattr(existing, "workspace", "") or "") != workspace_binding.root
                    or getattr(existing, "workspace_mode", None) != workspace_binding.mode
                    or getattr(existing, "workspace_state", None) != workspace_binding.state
                )
            needs_model_update = bool(
                model and (not getattr(existing, "model", None) or getattr(existing, "model", None) == "unknown")
            )
            needs_provider_update = bool(
                model_provider and not getattr(existing, "model_provider", None)
            )
            metadata_count = getattr(existing, "_metadata_message_count", None)
            needs_error_update = bool(cron_error_message) and not _has_matching_cron_error(
                getattr(existing, "messages", None),
                cron_error_message,
            )
            # A cron sidecar may have been materialized while its execution
            # database still lacked the final reply. Always enter the full-load
            # path so append-only reconciliation can repair that same-ID gap.
            needs_transcript_reconcile = True
            if not needs_update and not needs_model_update and not needs_provider_update and not needs_error_update and not needs_transcript_reconcile and (not fallback_output or (metadata_count or 0) > 0):
                return sid

            # load_metadata_only() returns messages=[] by design and Session.save()
            # refuses to persist that stub (#1558). Reload the full session before
            # patching materialized cron metadata so we never wipe transcripts.
            full = Session.load(sid)
            if full is None:
                return sid
            changed = False
            if needs_update:
                if not getattr(full, "project_id", None):
                    full.project_id = ensure_cron_project(profile=target_profile)
                full.is_cli_session = False
                full.source_tag = "cron"
                changed = True
            if workspace_binding is not None:
                if str(getattr(full, "workspace", "") or "") != workspace_binding.root:
                    if _can_repair_unverified_cron_workspace(full):
                        # The original import had no trustworthy root and used
                        # the shared continuation fallback.  Before a user has
                        # followed up, the canonical Agent record can repair
                        # that incomplete import without redirecting a live
                        # conversation.
                        full.workspace = workspace_binding.root
                        full.workspace_mode = workspace_binding.mode
                        full.workspace_state = workspace_binding.state
                        if workspace_binding.mode == "worktree":
                            full.worktree_path = workspace_binding.root
                            full.worktree_repo_root = workspace_binding.worktree_repo_root
                        else:
                            full.worktree_path = None
                            full.worktree_repo_root = None
                    else:
                        # A previously materialized root is immutable. Do not
                        # let a replay or malformed current-run input redirect
                        # a conversation that may already use it.
                        full.workspace_state = "workspace_unverified"
                    changed = True
                else:
                    full.workspace_mode = workspace_binding.mode
                    full.workspace_state = workspace_binding.state
                    if workspace_binding.mode == "worktree":
                        full.worktree_path = workspace_binding.root
                        full.worktree_repo_root = workspace_binding.worktree_repo_root
                    changed = True
            execution_source = execution_profile or target_profile
            if getattr(full, "cron_execution_profile", None) != execution_source:
                full.cron_execution_profile = execution_source
                changed = True
            if execution_ended_at is not None and getattr(full, "cron_execution_ended_at", None) != execution_ended_at:
                full.cron_execution_ended_at = execution_ended_at
                changed = True
            if reconcile_cron_session_transcript(
                full,
                job=job,
                fallback_output=fallback_output,
                run_mtime=run_mtime,
            ):
                changed = True
            if not (full.messages or []) and fallback_output:
                full.messages = build_cron_fallback_messages(
                    job,
                    fallback_output,
                    run_mtime=run_mtime,
                    end_reason=end_reason,
                )
                changed = True
            if _reconcile_cron_error(
                full.messages,
                cron_error_message,
                legacy_no_agent_detail=legacy_no_agent_detail,
            ):
                full.last_error_at = _cron_error_timestamp(error_timestamp)
                changed = True
            if needs_model_update:
                full.model = model
                changed = True
            if needs_provider_update:
                full.model_provider = model_provider
                changed = True
            if changed:
                full.save()
                try:
                    from api.models import LOCK, SESSIONS
                    with LOCK:
                        SESSIONS[sid] = full
                        SESSIONS.move_to_end(sid)
                except Exception:
                    logger.debug("failed to refresh cached cron session %s", sid, exc_info=True)
                publish_session_list_changed("cron_session_imported")
            return sid
        return sid

    msgs = get_state_db_session_messages(sid, profile=execution_profile or target_profile)
    if not msgs:
        msgs = get_state_db_session_messages(sid, profile=target_profile)
    if not msgs and fallback_output is not None:
        msgs = build_cron_fallback_messages(
            job,
            fallback_output,
            run_mtime=run_mtime,
            execution_error_detail=execution_error_detail,
            end_reason=end_reason,
        )
    _reconcile_cron_error(
        msgs,
        cron_error_message,
        legacy_no_agent_detail=legacy_no_agent_detail,
    )
    from integration.crons.hooks import (
        _stamp_cron_manifest_turn_keys,
        normalize_cron_manifest_messages,
    )

    msgs = _stamp_cron_manifest_turn_keys(
        normalize_cron_manifest_messages(
            msgs,
            collapse_execution_replayed_users=execution_ended_at is not None,
        )
    )

    title = (job or {}).get("name") or cli_title or f"Cron {str((job or {}).get('id') or '').strip()}"
    if workspace_binding is not None:
        imported_workspace = workspace_binding.root
        imported_workspace_mode = workspace_binding.mode
        imported_workspace_state = workspace_binding.state
    elif state_db_cwd:
        imported_workspace = state_db_cwd
        imported_workspace_mode = "external"
        imported_workspace_state = "legacy_shared"
    else:
        # A session object needs a path-shaped value.  The shared resolver
        # deliberately ignores it for this state and uses the approved default
        # workspace for continuation instead.
        from api.config import DEFAULT_WORKSPACE

        imported_workspace = str(DEFAULT_WORKSPACE)
        imported_workspace_mode = "external"
        imported_workspace_state = "workspace_unverified"

    s = import_cli_session(
        sid,
        title,
        msgs,
        model=model,
        model_provider=model_provider,
        profile=target_profile,
        created_at=started_at,
        updated_at=started_at,
        workspace=imported_workspace,
        workspace_mode=imported_workspace_mode,
        workspace_state=imported_workspace_state,
        require_workspace_binding=True,
    )
    s.project_id = ensure_cron_project(profile=target_profile)
    s.is_cli_session = False
    s.source_tag = "cron"
    if workspace_binding is not None and workspace_binding.mode == "worktree":
        s.worktree_path = workspace_binding.root
        s.worktree_repo_root = workspace_binding.worktree_repo_root
    s.cron_execution_profile = execution_profile or target_profile
    s.cron_execution_ended_at = execution_ended_at
    if cron_error_message:
        s.last_error_at = _cron_error_timestamp(error_timestamp)
    s.save()
    try:
        from api.models import LOCK, SESSIONS
        with LOCK:
            SESSIONS[sid] = s
            SESSIONS.move_to_end(sid)
    except Exception:
        logger.debug("failed to cache imported cron session %s", sid, exc_info=True)
    publish_session_list_changed("cron_session_imported")
    return sid


def _cron_session_cwd(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Read the Agent-owned cwd without assuming legacy state.db columns."""
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
        if "cwd" not in columns:
            return None
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    value = str(row[0] or "").strip()
    return value or None


def _current_run_workspace_binding(job: dict, session_id: str, state_db_cwd: str | None):
    """Build a V1 binding from the selected Agent record for this run.

    In-process/manual runs retain the explicit execution hand-off.  Gateway
    polling and recovery reload jobs.json after that transient hand-off has
    disappeared, so their exact selected ``source=cron`` state.db session is
    the corresponding durable identity and canonical cwd.
    """
    if not isinstance((job or {}).get("workspace_policy"), dict):
        return None
    try:
        from integration.crons.workspace_policy import binding_for_current_run

        expected_session_id = str((job or {}).get("_cron_session_id") or "").strip()
        if expected_session_id and expected_session_id != session_id:
            raise ValueError("当前运行的 session ID 不一致")
        binding_job = job if expected_session_id else {**(job or {}), "_cron_session_id": session_id}
        execution_cwd = (job or {}).get("_cron_execution_workspace") or state_db_cwd

        return binding_for_current_run(
            binding_job,
            session_id=session_id,
            cwd=execution_cwd,
            state_db_cwd=state_db_cwd,
        )
    except Exception as exc:
        logger.warning("cron workspace binding rejected for session_id=%s: %s", session_id, exc)
        return None


def _legacy_workspace_binding(
    job: dict,
    *,
    state_db_cwd: str | None,
    profile_home: Path | None,
):
    """Create a follow-up-only binding for a job that predates V1 policy."""
    if isinstance((job or {}).get("workspace_policy"), dict):
        return None
    try:
        from integration.crons.workspace_policy import binding_for_legacy_session

        return binding_for_legacy_session(
            job,
            state_db_cwd=state_db_cwd,
            profile_home=profile_home,
        )
    except Exception as exc:
        logger.warning("legacy cron workspace binding rejected: %s", exc)
        return None


def materialize_cron_session(
    job: dict,
    *,
    owner_profile: str,
    execution_home: Path,
    run_mtime: float | None = None,
    session_id: str | None = None,
    fallback_output: str | None = None,
    fallback_filename: str | None = None,
    execution_end_reason: str | None = None,
    execution_error_detail: str | None = None,
    execution_ended_at: float | None = None,
) -> str | None:
    """Import the latest cron session from execution state.db into target_profile."""
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id:
        return None

    if fallback_output is None and (run_mtime is not None or fallback_filename):
        fallback_output, fallback_filename = read_cron_output_for_run(
            job_id,
            run_mtime=run_mtime,
            filename=fallback_filename,
        )

    owner = _normalize_profile_name(owner_profile)
    target_profile = _target_profile_for_job(job, owner)
    execution_profile = _execution_profile_name(job)

    fallback_record = None
    if fallback_output is not None and fallback_filename:
        fallback_record = _build_cron_output_run_record(
            job,
            filename=fallback_filename,
            output=fallback_output,
            ended_at=run_mtime if run_mtime is not None else time.time(),
            end_reason=execution_end_reason,
            execution_error_detail=execution_error_detail,
        )
    elif (job or {}).get("no_agent") and session_id and execution_end_reason:
        fallback_record = _build_cron_runtime_result_record(
            job,
            session_id=session_id,
            ended_at=execution_ended_at if execution_ended_at is not None else time.time(),
            end_reason=execution_end_reason,
            execution_error_detail=execution_error_detail,
        )

    def materialize_output_fallback() -> str | None:
        if fallback_record is None:
            return None
        workspace_binding = _legacy_workspace_binding(
            job,
            state_db_cwd=None,
            profile_home=Path(execution_home),
        )
        return _materialize_and_settle_cron_session_found(
            job,
            (
                str(session_id or fallback_record["id"]),
                str(fallback_record["title"]),
                fallback_record["started_at"],
                str(fallback_record.get("model") or ""),
            ),
            target_profile=target_profile,
            execution_profile=execution_profile,
            fallback_output=fallback_output,
            run_mtime=fallback_record["ended_at"],
            end_reason=fallback_record.get("end_reason"),
            execution_ended_at=execution_ended_at,
            execution_error_detail=execution_error_detail,
            workspace_binding=workspace_binding,
            state_db_cwd=None,
        )

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return materialize_output_fallback()

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            found = _select_cron_session_for_run(
                conn,
                job_id,
                run_mtime=run_mtime,
                session_id=session_id,
            )
            end_reason, ended_at = _cron_session_completion(conn, found[0]) if found else (None, None)
            state_db_cwd = _cron_session_cwd(conn, found[0]) if found else None
    except sqlite3.Error as exc:
        logger.debug("materialize_cron_session: state.db read failed: %s", exc)
        return materialize_output_fallback()

    if not found and (job or {}).get("no_agent") and fallback_record is not None:
        _import_cron_output_run_records(db_path, [fallback_record], job_id=job_id)
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                found = _select_cron_session_for_run(
                    conn,
                    job_id,
                    run_mtime=run_mtime,
                    session_id=fallback_record["id"],
                )
                end_reason, ended_at = (
                    _cron_session_completion(conn, found[0]) if found else (None, None)
                )
                state_db_cwd = _cron_session_cwd(conn, found[0]) if found else None
        except sqlite3.Error as exc:
            logger.debug("materialize_cron_session: synthesized state.db read failed: %s", exc)

    if not found:
        return materialize_output_fallback()

    current_v1 = isinstance((job or {}).get("workspace_policy"), dict)
    workspace_binding = _current_run_workspace_binding(job, found[0], state_db_cwd)
    if not current_v1:
        workspace_binding = _legacy_workspace_binding(
            job,
            state_db_cwd=state_db_cwd,
            profile_home=Path(execution_home),
        )

    return _materialize_and_settle_cron_session_found(
        job,
        found,
        target_profile=target_profile,
        execution_profile=execution_profile,
        fallback_output=fallback_output,
        run_mtime=run_mtime,
        end_reason=end_reason,
        execution_ended_at=ended_at,
        execution_error_detail=execution_error_detail,
        workspace_binding=workspace_binding,
        state_db_cwd=state_db_cwd if workspace_binding is not None or not current_v1 else None,
    )


def materialize_cron_session_run(
    job: dict,
    *,
    owner_profile: str,
    run: dict[str, Any],
    fallback_output: str | None = None,
    history_read: bool = False,
) -> str | None:
    """Materialize one already-selected database run for WebUI session viewing."""
    sid = str((run or {}).get("session_id") or "").strip()
    if not sid:
        return None
    owner = _normalize_profile_name(owner_profile)
    found = (
        sid,
        str((run or {}).get("title") or ""),
        (run or {}).get("started_at"),
        str((run or {}).get("model") or ""),
    )
    is_v1_job = isinstance((job or {}).get("workspace_policy"), dict)
    execution_profile = _execution_profile_name(job) or owner
    target_profile = _target_profile_for_job(job, owner)
    if history_read:
        try:
            from api.models import Session

            sidecar = Session.load_metadata_only(sid)
            if _cron_history_sidecar_is_complete(
                sidecar,
                job=job,
                target_profile=target_profile,
                execution_profile=execution_profile,
                run=run,
            ):
                return sid
        except Exception:
            logger.debug("cron history sidecar check failed for session_id=%s", sid, exc_info=True)
    try:
        profile_home = Path(_profile_home_for_name(execution_profile))
    except Exception:
        profile_home = None
    state_db_cwd = None
    if profile_home is not None:
        db_path = profile_home / "state.db"
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                selected = _select_cron_session_for_run(
                    conn,
                    str((job or {}).get("id") or ""),
                    session_id=sid,
                )
                if selected is not None and selected[0] == sid:
                    state_db_cwd = _cron_session_cwd(conn, sid)
        except sqlite3.Error as exc:
            logger.debug("materialize_cron_session_run: state.db read failed: %s", exc)
    if is_v1_job:
        # The selected record is an exact source=cron session for this job, so
        # its canonical cwd is as trustworthy as an in-process hand-off.
        workspace_binding = _current_run_workspace_binding(job, sid, state_db_cwd)
    else:
        workspace_binding = _legacy_workspace_binding(
            job,
            state_db_cwd=state_db_cwd,
            profile_home=profile_home,
        )
    return _materialize_and_settle_cron_session_found(
        job,
        found,
        target_profile=target_profile,
        execution_profile=execution_profile,
        fallback_output=fallback_output,
        run_mtime=(run or {}).get("ended_at") or (run or {}).get("started_at"),
        end_reason=(run or {}).get("end_reason"),
        execution_ended_at=(run or {}).get("ended_at"),
        execution_error_detail=(run or {}).get("execution_error_detail") or (run or {}).get("error"),
        workspace_binding=workspace_binding,
        state_db_cwd=state_db_cwd if workspace_binding is not None or not is_v1_job else None,
    )


def materialize_cron_sessions_for_runs(
    job: dict,
    *,
    owner_profile: str,
    execution_home: Path,
    runs: list[dict[str, Any]],
) -> dict[str, str]:
    """Batch materialize cron runs and return ``filename -> session_id``.

    History endpoints call this to avoid opening ``state.db`` and scanning all
    candidate cron sessions once per output file.
    """
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id or not runs:
        return {}

    owner = _normalize_profile_name(owner_profile)
    target_profile = _target_profile_for_job(job, owner)
    execution_profile = _execution_profile_name(job)
    is_v1_job = isinstance((job or {}).get("workspace_policy"), dict)

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return {}

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            candidates = _cron_session_candidates(conn, job_id)
            completions = {
                sid: _cron_session_completion(conn, sid)
                for sid, _, _, _ in candidates
            }
            cwd_by_session = {
                sid: _cron_session_cwd(conn, sid)
                for sid, _, _, _ in candidates
            }
    except sqlite3.Error as exc:
        logger.debug("materialize_cron_sessions_for_runs: state.db read failed: %s", exc)
        return {}
    if not candidates:
        return {}

    session_ids: dict[str, str] = {}
    for run in runs:
        filename = str(run.get("filename") or "")
        try:
            run_mtime = run.get("run_mtime")
            if run_mtime is not None:
                run_mtime = float(run_mtime)
        except (TypeError, ValueError):
            run_mtime = None
        found = _select_cron_session_candidate(candidates, run_mtime=run_mtime)
        if not found:
            continue
        state_db_cwd = cwd_by_session.get(found[0])
        if is_v1_job:
            # ``found`` came from this job's source=cron candidate set, so the
            # corresponding state.db cwd is a verified V1 execution record.
            workspace_binding = _current_run_workspace_binding(job, found[0], state_db_cwd)
        else:
            workspace_binding = _legacy_workspace_binding(
                job,
                state_db_cwd=state_db_cwd,
                profile_home=Path(execution_home),
            )
        sid = _materialize_and_settle_cron_session_found(
            job,
            found,
            target_profile=target_profile,
            execution_profile=execution_profile,
            fallback_output=run.get("fallback_output"),
            run_mtime=run_mtime,
            end_reason=completions.get(found[0], (None, None))[0],
            execution_ended_at=completions.get(found[0], (None, None))[1],
            workspace_binding=workspace_binding,
            state_db_cwd=state_db_cwd if workspace_binding is not None or not is_v1_job else None,
        )
        if filename:
            session_ids[filename] = sid
    return session_ids


def materialize_cron_session_by_job_id(owner_profile: str, job_id: str) -> str | None:
    """Compensating import when only jobs.json was updated (e.g. external Gateway)."""
    from cron.jobs import get_job

    owner = _normalize_profile_name(owner_profile)
    if not owner:
        owner = resolve_owner_profile_for_job(job_id) or ""
    if not owner:
        return None

    from api.profiles import cron_profile_context_for_home

    with cron_profile_context_for_home(_profile_home_for_name(owner)):
        job = get_job(job_id)
    if not job:
        return None

    from api.routes import _profile_home_for_cron_job

    execution_home = _profile_home_for_cron_job(job)
    return materialize_cron_session(job, owner_profile=owner, execution_home=execution_home)
