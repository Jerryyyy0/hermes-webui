"""Unread run tracking for Cron Hub."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from api.profiles import cron_profile_context_for_home, list_profiles_api

from integration.crons.listing import _normalize_profile_name, _profile_home_for_name

logger = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()
_SAFE_JOB_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}")


def _state_path() -> Path:
    from api.profiles import _DEFAULT_HERMES_HOME

    return Path(_DEFAULT_HERMES_HOME) / "integration" / "cron_unread.json"


def _job_key(profile: str, job_id: str) -> str:
    return f"{_normalize_profile_name(profile)}:{job_id}"


def _validate_job_id(job_id: str) -> str:
    value = str(job_id or "").strip()
    if not value or not _SAFE_JOB_ID.fullmatch(value) or value in (".", ".."):
        raise ValueError("invalid job_id")
    return value


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "read": {}}
    except (OSError, json.JSONDecodeError):
        logger.debug("Failed to read Cron Hub unread state from %s", path, exc_info=True)
        return {"version": 1, "read": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "read": {}}
    read = payload.get("read")
    if not isinstance(read, dict):
        payload["read"] = {}
    payload["version"] = 1
    return payload


def _write_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_until_for(state: dict[str, Any], profile: str, job_id: str) -> float:
    row = state.get("read", {}).get(_job_key(profile, job_id), {})
    if not isinstance(row, dict):
        return 0.0
    try:
        return float(row.get("read_until") or 0)
    except (TypeError, ValueError):
        return 0.0


def _run_files(profile_home: Path, job_id: str) -> list[dict[str, Any]]:
    out_dir = profile_home / "cron" / "output" / job_id
    if not out_dir.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for file in out_dir.glob("*.md"):
        try:
            st = file.stat()
        except OSError:
            logger.debug("Failed to stat cron output file %s", file)
            continue
        runs.append({"filename": file.name, "modified": float(st.st_mtime)})
    runs.sort(key=lambda row: row["modified"], reverse=True)
    return runs


def _jobs_for_profile(profile_name: str, profile_home: Path) -> list[dict[str, Any]]:
    try:
        from cron.jobs import list_jobs

        with cron_profile_context_for_home(profile_home):
            jobs = list_jobs(include_disabled=True)
        return [job for job in jobs if isinstance(job, dict)]
    except Exception:
        logger.debug("Failed to list cron jobs for unread profile %s", profile_name, exc_info=True)
        return []


def unread_summary_all_profiles() -> dict[str, Any]:
    """Return unread Cron Hub run counts across all known profiles."""

    with _STATE_LOCK:
        state = _read_state()

    rows: list[dict[str, Any]] = []
    total_unread = 0
    for profile_row in list_profiles_api():
        owner = _normalize_profile_name(profile_row.get("name"))
        home = _profile_home_for_name(owner)
        for job in _jobs_for_profile(owner, home):
            job_id = str(job.get("id") or "").strip()
            try:
                job_id = _validate_job_id(job_id)
            except ValueError:
                continue
            runs = _run_files(home, job_id)
            if not runs:
                continue
            read_until = _read_until_for(state, owner, job_id)
            unread_runs = [run for run in runs if run["modified"] > read_until]
            if not unread_runs:
                continue
            latest = runs[0]
            unread_count = len(unread_runs)
            total_unread += unread_count
            rows.append(
                {
                    "profile": owner,
                    "job_id": job_id,
                    "name": job.get("name") or job_id,
                    "unread_count": unread_count,
                    "latest_completed_at": latest["modified"],
                    "latest_filename": latest["filename"],
                }
            )

    rows.sort(key=lambda row: row.get("latest_completed_at") or 0, reverse=True)
    return {"unread_count": total_unread, "jobs": rows}


def mark_job_read(profile: str, job_id: str) -> dict[str, Any]:
    """Mark all current runs for one Cron Hub job as read."""

    owner = _normalize_profile_name(profile)
    job_id = _validate_job_id(job_id)
    home = _profile_home_for_name(owner)
    jobs = _jobs_for_profile(owner, home)
    if not any(str(job.get("id") or "") == job_id for job in jobs):
        raise KeyError("Job not found")

    runs = _run_files(home, job_id)
    latest = runs[0] if runs else None
    key = _job_key(owner, job_id)
    with _STATE_LOCK:
        state = _read_state()
        read = state.setdefault("read", {})
        if latest:
            read[key] = {
                "read_until": latest["modified"],
                "filename": latest["filename"],
            }
        else:
            read.pop(key, None)
        _write_state(state)

    return {
        "ok": True,
        "profile": owner,
        "job_id": job_id,
        "read_until": latest["modified"] if latest else None,
        "latest_filename": latest["filename"] if latest else None,
    }
