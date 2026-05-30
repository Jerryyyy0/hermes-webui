"""Cross-profile cron job listing and completion aggregation."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_profile_name(name: str | None) -> str:
    if not name or not str(name).strip():
        return "default"
    return str(name).strip()


def _profile_home_for_name(profile_name: str) -> Path:
    from api.profiles import get_hermes_home_for_profile, _is_root_profile, _DEFAULT_HERMES_HOME

    if _is_root_profile(profile_name):
        return Path(_DEFAULT_HERMES_HOME)
    return Path(get_hermes_home_for_profile(profile_name))


def _cron_job_for_api(job: dict) -> dict:
    from api.routes import _cron_job_for_api

    return _cron_job_for_api(job)


def list_jobs_all_profiles(*, include_disabled: bool = True) -> dict[str, Any]:
    """Aggregate cron jobs from every known profile into grouped response."""
    from api.profiles import cron_profile_context_for_home, list_profiles_api
    from cron.jobs import list_jobs

    profiles_out: list[dict[str, Any]] = []
    for profile_row in list_profiles_api():
        profile_name = _normalize_profile_name(profile_row.get("name"))
        try:
            with cron_profile_context_for_home(_profile_home_for_name(profile_name)):
                jobs = list_jobs(include_disabled=include_disabled)
                profiles_out.append(
                    {
                        "profile": profile_name,
                        "jobs": [_cron_job_for_api(j) for j in jobs],
                    }
                )
        except Exception as exc:
            logger.debug("list_jobs_all_profiles: skip profile %s: %s", profile_name, exc)

    return {
        "all_profiles": True,
        "profiles": profiles_out,
    }


def _parse_last_run_timestamp(last_run) -> float | None:
    if last_run is None:
        return None
    if isinstance(last_run, (int, float)):
        return float(last_run)
    if isinstance(last_run, str):
        try:
            return datetime.datetime.fromisoformat(
                last_run.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def recent_completions_all_profiles(since: float) -> dict[str, Any]:
    """Aggregate cron completions across profiles since timestamp."""
    from api.profiles import cron_profile_context_for_home, list_profiles_api
    from cron.jobs import list_jobs

    completions: list[dict[str, Any]] = []
    for profile_row in list_profiles_api():
        profile_name = _normalize_profile_name(profile_row.get("name"))
        try:
            with cron_profile_context_for_home(_profile_home_for_name(profile_name)):
                jobs = list_jobs(include_disabled=True)
                for job in jobs:
                    last_run = job.get("last_run_at")
                    if last_run is None:
                        continue
                    ts = _parse_last_run_timestamp(last_run)
                    if ts is None:
                        continue
                    if ts > since:
                        completions.append(
                            {
                                "job_id": job.get("id", ""),
                                "name": job.get("name", "Unknown"),
                                "status": job.get("last_status", "unknown"),
                                "completed_at": ts,
                                "toast_notifications": job.get("toast_notifications") is not False,
                                "owner_profile": profile_name,
                                "profile": job.get("profile"),
                            }
                        )
        except Exception as exc:
            logger.debug(
                "recent_completions_all_profiles: skip profile %s: %s", profile_name, exc
            )

    return {
        "since": since,
        "completions": completions,
    }


def resolve_owner_profile_for_job(job_id: str) -> str | None:
    """Find which profile's cron/jobs.json contains job_id."""
    from api.profiles import list_profiles_api

    needle = str(job_id or "").strip()
    if not needle:
        return None
    for profile_row in list_profiles_api():
        profile_name = _normalize_profile_name(profile_row.get("name"))
        jobs_file = _profile_home_for_name(profile_name) / "cron" / "jobs.json"
        if not jobs_file.is_file():
            continue
        try:
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            continue
        if any(str(j.get("id") or "") == needle for j in jobs if isinstance(j, dict)):
            return profile_name
    return None
