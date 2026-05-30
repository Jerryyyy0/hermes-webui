"""HTTP handlers for integration cron routes (/api/integration/crons/*)."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from api.helpers import bad, j, require
from api.profiles import cron_profile_context_for_home, get_hermes_home_for_profile
from api.routes import _cron_job_for_api, _normalize_cron_profile_value, _run_cron_tracked

from integration.config import cron_all_profiles_enabled
from integration.crons.listing import _normalize_profile_name


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, msg, status: int = 400) -> bool:
    bad(handler, msg, status=status)
    return True


@contextmanager
def _owner_cron_context(owner_profile: str):
    owner = _normalize_profile_name(owner_profile)
    with cron_profile_context_for_home(get_hermes_home_for_profile(owner)):
        yield owner


def try_handle_get(handler, parsed) -> bool:
    return False


def try_handle_post(handler, parsed, body) -> bool:
    if not cron_all_profiles_enabled():
        return False
    path = parsed.path
    if path == "/api/integration/crons/create":
        return _handle_create(handler, body)
    if path == "/api/integration/crons/update":
        return _handle_update(handler, body)
    if path == "/api/integration/crons/delete":
        return _handle_delete(handler, body)
    if path == "/api/integration/crons/run":
        return _handle_run(handler, body)
    if path == "/api/integration/crons/pause":
        return _handle_pause(handler, body)
    if path == "/api/integration/crons/resume":
        return _handle_resume(handler, body)
    return False


def _handle_create(handler, body):
    try:
        require(body, "prompt", "schedule")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")
    try:
        profile = _normalize_cron_profile_value(body.get("profile"))
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import create_job, update_job

    toast_notifications = body.get("toast_notifications") is not False
    try:
        with _owner_cron_context(owner_profile) as owner:
            job = create_job(
                prompt=body["prompt"],
                schedule=body["schedule"],
                name=body.get("name") or None,
                deliver=body.get("deliver") or "local",
                skills=body.get("skills") or [],
                model=body.get("model") or None,
            )
            post_create: dict = {}
            if profile is not None:
                post_create["profile"] = profile
            if not toast_notifications:
                post_create["toast_notifications"] = False
            if post_create:
                job = update_job(job["id"], post_create) or job
            return _respond(
                handler,
                {"ok": True, "owner_profile": owner, "job": _cron_job_for_api(job)},
            )
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_update(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")
    try:
        _normalize_cron_profile_value(body.get("profile"))
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import update_job

    updates = {k: v for k, v in body.items() if k not in ("job_id", "owner_profile") and v is not None}
    if "profile" in updates:
        updates["profile"] = _normalize_cron_profile_value(updates.get("profile"))
    try:
        with _owner_cron_context(owner_profile) as owner:
            job = update_job(body["job_id"], updates)
            if not job:
                return _respond_bad(handler, "Job not found", 404)
            return _respond(
                handler,
                {"ok": True, "owner_profile": owner, "job": _cron_job_for_api(job)},
            )
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_delete(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")

    from cron.jobs import remove_job

    with _owner_cron_context(owner_profile):
        ok = remove_job(body["job_id"])
    if not ok:
        return _respond_bad(handler, "Job not found", 404)
    return _respond(handler, {"ok": True, "job_id": body["job_id"]})


def _handle_run(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")

    from cron.jobs import get_job
    from api.routes import _is_cron_running, _mark_cron_running, _profile_home_for_cron_job

    with _owner_cron_context(owner_profile):
        job = get_job(body["job_id"])
    if not job:
        return _respond_bad(handler, "Job not found", 404)

    already_running, elapsed = _is_cron_running(body["job_id"])
    if already_running:
        return _respond(
            handler,
            {
                "ok": False,
                "job_id": body["job_id"],
                "status": "already_running",
                "elapsed": round(elapsed, 1),
            },
        )

    _mark_cron_running(body["job_id"])
    storage_home = get_hermes_home_for_profile(owner_profile)
    execution_home = _profile_home_for_cron_job(job)
    threading.Thread(
        target=_run_cron_tracked,
        args=(job, storage_home, execution_home, owner_profile),
        daemon=True,
    ).start()
    return _respond(handler, {"ok": True, "job_id": body["job_id"], "status": "running"})


def _handle_pause(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")

    from cron.jobs import pause_job

    with _owner_cron_context(owner_profile):
        result = pause_job(body["job_id"], reason=body.get("reason"))
    if result:
        return _respond(handler, {"ok": True, "job": _cron_job_for_api(result)})
    return _respond_bad(handler, "Job not found", 404)


def _handle_resume(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    owner_profile = _normalize_profile_name(body.get("owner_profile"))
    if not owner_profile:
        return _respond_bad(handler, "owner_profile required")

    from cron.jobs import resume_job

    with _owner_cron_context(owner_profile):
        result = resume_job(body["job_id"])
    if result:
        return _respond(handler, {"ok": True, "job": _cron_job_for_api(result)})
    return _respond_bad(handler, "Job not found", 404)
