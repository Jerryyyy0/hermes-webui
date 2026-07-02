"""HTTP handler for GET /api/integration/memory/stats.

Aggregates session statistics, cron job counts, and memory file listings
for a given profile.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from api.helpers import bad, j
from api.profiles import (
    cron_profile_context_for_home,
    get_active_hermes_home,
    get_active_profile_name,
    get_hermes_home_for_profile,
    list_profiles_api,
)

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_profile_home(profile_name: str | None) -> tuple[str, str]:
    """Return (resolved_profile_name, hermes_home_path_str).

    When *profile_name* is None or empty, uses the currently active profile.
    Validates that the named profile exists.
    """
    if profile_name:
        name = profile_name.strip()
        # Validate the profile exists
        known = {p.get("name") for p in list_profiles_api()}
        known.add("default")
        if name not in known:
            raise ValueError(f"未知的 profile: {name}")
        home = get_hermes_home_for_profile(name)
        return name, str(home)
    # Fall back to active profile
    return get_active_profile_name(), str(get_active_hermes_home())


def _collect_session_stats(sessions: list[dict]) -> dict:
    """Build stats from a list of session rows (sidebar-format dicts)."""
    dates: set[str] = set()
    import datetime

    for s in sessions:
        ts = s.get("last_message_at") or s.get("updated_at") or s.get("created_at")
        if ts:
            try:
                dt = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
                dates.add(dt.strftime("%Y-%m-%d"))
            except (ValueError, TypeError, OSError):
                pass

    return {
        "session_days": sorted(dates),
        "session_count": len(sessions),
    }


def _collect_memory_files(home_path: str) -> list[str]:
    """Scan <home>/memories/ directory and return filenames.

    Filters out lock files (*.lock) and system files (._*).
    """
    from pathlib import Path

    mem_dir = Path(home_path) / "memories"
    if not mem_dir.is_dir():
        return []
    files: list[str] = []
    for entry in sorted(mem_dir.iterdir()):
        if entry.is_file():
            name = entry.name
            # Skip lock files and AppleDouble metadata files
            if name.endswith(".lock") or name.startswith("._"):
                continue
            files.append(name)
    return files


def _collect_cron_stats(home_path: str) -> tuple[int, list[dict]]:
    """Fetch cron jobs for a given profile home.

    Returns (count, cron_jobs_list).
    """
    from pathlib import Path

    try:
        from cron.jobs import list_jobs
    except ImportError:
        return 0, []

    try:
        with cron_profile_context_for_home(Path(home_path)):
            jobs = list_jobs(include_disabled=True)
    except Exception:
        logger.debug("memory_stats: failed to list cron jobs for %s", home_path, exc_info=True)
        return 0, []

    # Format jobs — inline a minimal version so we don't depend on routes.py
    formatted = [_format_cron_job(j) for j in (jobs or [])]
    return len(formatted), formatted


def _format_cron_job(job: dict) -> dict:
    """Normalize a cron job dict for API output."""
    payload = dict(job or {})
    payload.setdefault("profile", None)
    payload["toast_notifications"] = payload.get("toast_notifications") is not False
    # Execution status
    execution_bucket, execution_state = _cron_execution_status(payload)
    payload["execution_bucket"] = execution_bucket
    payload["execution_state"] = execution_state
    return payload


def _cron_execution_status(job: dict) -> tuple[str, str]:
    """Return (execution_bucket, execution_state) pair for a cron job.

    Mirrors api.routes._cron_execution_status_for_api logic.
    """
    import time

    try:
        from api.routes import _is_cron_running
    except ImportError:
        _is_cron_running = lambda _: (False, 0.0)  # noqa: E731

    job_id = str((job or {}).get("id", ""))
    if not job_id:
        return "unknown", "unknown"

    running, _elapsed = _is_cron_running(job_id)
    if running:
        return "running", "running"

    status = str((job or {}).get("last_status", "") or "")
    if status == "success":
        return "success", "success"
    if status == "error":
        return "error", "schedule_error"
    return "waiting", "scheduled_waiting"


# ── sessions fetching ────────────────────────────────────────────────────────


def _fetch_profile_sessions(home_path: str, profile_name: str) -> list[dict]:
    """Fetch and scope sessions for a profile, using cron profile context."""
    from api.config import load_settings
    from api.models import all_sessions
    from api.routes import finalize_sessions_for_profile, _redact_sidebar_session_rows

    settings = load_settings()
    merged = all_sessions()
    scoped = finalize_sessions_for_profile(
        merged,
        settings=settings,
        profile_name=profile_name,
    )
    return _redact_sidebar_session_rows(scoped)


# ── handler ───────────────────────────────────────────────────────────────────


def try_handle_get(handler, parsed) -> bool:
    if parsed.path != "/api/integration/memory/stats":
        return False

    qs = parse_qs(parsed.query)
    raw_profile = qs.get("profile", [None])[0]

    try:
        profile_name, home_path = _resolve_profile_home(raw_profile)
    except ValueError as e:
        return bad(handler, str(e), status=400)

    # 1. Sessions
    sessions = _fetch_profile_sessions(home_path, profile_name)
    stats = _collect_session_stats(sessions)

    # 2. Crons
    cron_count, crons = _collect_cron_stats(home_path)
    stats["cron_count"] = cron_count

    # 3. Memory files
    memory_files = _collect_memory_files(home_path)

    payload = {
        "profile": profile_name,
        "stats": stats,
        "sessions": sessions,
        "crons": crons,
        "memory_files": memory_files,
    }

    return j(handler, payload)