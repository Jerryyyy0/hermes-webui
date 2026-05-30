"""Import cron agent sessions into WebUI sidecar and expose them in the sidebar."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def cron_sessions_visible_in_sidebar(session: dict) -> bool:
    """Materialized cron sessions (WebUI copy) are visible; raw CLI cron rows stay hidden."""
    if not cron_all_profiles_enabled():
        return False
    source = session.get("source_tag") or session.get("source")
    if source != "cron":
        return False
    return session.get("is_cli_session") is False


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


def _cron_session_candidates(conn, job_id: str) -> list[tuple[str, str, float | None]]:
    pattern = f"cron_{job_id}_%"
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sessions)")
    cols = {str(row[1]) for row in cur.fetchall()}
    if "source" not in cols:
        return []
    order = "started_at DESC" if "started_at" in cols else "id DESC"
    cur.execute(
        f"""
        SELECT id, title, started_at FROM sessions
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
        rows.append((sid, title, started))
    return rows


def _latest_cron_session_id(conn, job_id: str) -> tuple[str, str, float | None] | None:
    candidates = _cron_session_candidates(conn, job_id)
    return candidates[0] if candidates else None


def _select_cron_session_candidate(
    candidates: list[tuple[str, str, float | None]],
    *,
    run_mtime: float | None = None,
) -> tuple[str, str, float | None] | None:
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
) -> tuple[str, str, float | None] | None:
    candidates = _cron_session_candidates(conn, job_id)
    return _select_cron_session_candidate(candidates, run_mtime=run_mtime)


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
) -> list[dict[str, Any]]:
    """Synthetic user/assistant pair when state.db has no cron run messages."""
    prompt = str((job or {}).get("prompt") or "")
    body = _cron_output_body(output_content)
    if not body:
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


def _materialize_cron_session_found(
    job: dict,
    found: tuple[str, str, float | None],
    *,
    target_profile: str,
    execution_profile: str | None,
    fallback_output: str | None = None,
    run_mtime: float | None = None,
) -> str:
    sid, cli_title, started_at = found

    from api.models import Session, ensure_cron_project, import_cli_session
    from api.models import get_state_db_session_messages
    from api.session_events import publish_session_list_changed

    existing = Session.load_metadata_only(sid)
    if existing is not None:
        existing_profile = getattr(existing, "profile", None) or ""
        if _normalize_profile_name(existing_profile) == target_profile:
            needs_update = (
                not getattr(existing, "project_id", None)
                or getattr(existing, "is_cli_session", None) is not False
                or getattr(existing, "source_tag", None) != "cron"
            )
            metadata_count = getattr(existing, "_metadata_message_count", None)
            if not needs_update and (not fallback_output or (metadata_count or 0) > 0):
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
            if not (full.messages or []) and fallback_output:
                full.messages = build_cron_fallback_messages(
                    job,
                    fallback_output,
                    run_mtime=run_mtime,
                )
                changed = True
            if changed:
                full.save()
                publish_session_list_changed("cron_session_imported")
            return sid
        return sid

    msgs = get_state_db_session_messages(sid, profile=execution_profile or target_profile)
    if not msgs:
        msgs = get_state_db_session_messages(sid, profile=target_profile)
    if not msgs and fallback_output:
        msgs = build_cron_fallback_messages(
            job,
            fallback_output,
            run_mtime=run_mtime,
        )

    title = (job or {}).get("name") or cli_title or f"Cron {str((job or {}).get('id') or '').strip()}"
    s = import_cli_session(
        sid,
        title,
        msgs,
        profile=target_profile,
        created_at=started_at,
        updated_at=started_at,
    )
    s.project_id = ensure_cron_project(profile=target_profile)
    s.is_cli_session = False
    s.source_tag = "cron"
    s.save()
    publish_session_list_changed("cron_session_imported")
    return sid


def materialize_cron_session(
    job: dict,
    *,
    owner_profile: str,
    execution_home: Path,
    run_mtime: float | None = None,
    fallback_output: str | None = None,
    fallback_filename: str | None = None,
) -> str | None:
    """Import the latest cron session from execution state.db into target_profile."""
    if not cron_all_profiles_enabled():
        return None
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

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return None

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            found = _select_cron_session_for_run(conn, job_id, run_mtime=run_mtime)
    except sqlite3.Error as exc:
        logger.debug("materialize_cron_session: state.db read failed: %s", exc)
        return None

    if not found:
        return None

    return _materialize_cron_session_found(
        job,
        found,
        target_profile=target_profile,
        execution_profile=execution_profile,
        fallback_output=fallback_output,
        run_mtime=run_mtime,
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
    if not cron_all_profiles_enabled():
        return {}
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id or not runs:
        return {}

    owner = _normalize_profile_name(owner_profile)
    target_profile = _target_profile_for_job(job, owner)
    execution_profile = _execution_profile_name(job)

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return {}

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            candidates = _cron_session_candidates(conn, job_id)
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
        sid = _materialize_cron_session_found(
            job,
            found,
            target_profile=target_profile,
            execution_profile=execution_profile,
            fallback_output=run.get("fallback_output"),
            run_mtime=run_mtime,
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
