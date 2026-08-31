"""HTTP handlers for integration cron routes (/api/integration/crons/*)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

from api.helpers import _sanitize_error, bad, j, require
from api.profiles import cron_profile_context_for_home, get_hermes_home_for_profile
from api.routes import _cron_job_for_api, _normalize_cron_profile_value, _run_cron_tracked

from integration.config import cron_all_profiles_enabled
from integration.crons.listing import _normalize_profile_name


_UPDATE_FIELDS = {
    "name", "prompt", "schedule", "schedule_display", "repeat", "deliver",
    "skills", "skill", "model", "provider", "base_url", "script", "no_agent",
    "context_from", "enabled_toolsets", "workdir", "enabled", "state",
    "paused_at", "paused_reason", "workspace_policy", "toast_notifications",
    "idle_window",
}

_IDLE_WINDOW_SCHEDULE_KINDS = {"once", "cron"}


def _normalize_idle_window_schedule(value, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"idle_window.{field_name} 必须为对象")

    kind = value.get("kind")
    if kind not in _IDLE_WINDOW_SCHEDULE_KINDS:
        raise ValueError(f"idle_window.{field_name}.kind 仅支持 once 或 cron")

    if kind == "once":
        if set(value) - {"kind", "run_at", "display"} or "run_at" not in value:
            raise ValueError(f"idle_window.{field_name} 必须包含 kind 和 run_at")
        run_at = value["run_at"]
        if not isinstance(run_at, str):
            raise ValueError(f"idle_window.{field_name}.run_at 必须为 ISO 8601 时间字符串")
        try:
            parsed = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"idle_window.{field_name}.run_at 必须为 ISO 8601 时间字符串") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"idle_window.{field_name}.run_at 必须包含时区偏移")
        normalized = {"kind": "once", "run_at": parsed.isoformat()}
    else:
        if set(value) - {"kind", "expr", "display"} or "expr" not in value:
            raise ValueError(f"idle_window.{field_name} 必须包含 kind 和 expr")
        expr = value["expr"]
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError(f"idle_window.{field_name}.expr 必须为非空字符串")
        normalized = {"kind": "cron", "expr": expr.strip()}

    display = value.get("display")
    if display is not None and (not isinstance(display, str) or not display.strip()):
        raise ValueError(f"idle_window.{field_name}.display 必须为非空字符串")
    normalized["display"] = display.strip() if isinstance(display, str) else normalized.get("run_at", normalized.get("expr"))
    return normalized


def _normalize_idle_window(body: dict, *, default=None):
    """Validate Cron Hub-only idle-window metadata as an atomic object."""
    if {"idle_start_time", "idle_end_time", "start_time", "end_time"}.intersection(body):
        raise ValueError("不支持平铺时间字段，请使用 idle_window.start_schedule 和 end_schedule")
    if "idle_window" not in body:
        return default

    idle_window = body["idle_window"]
    if idle_window is None:
        return None
    if not isinstance(idle_window, dict):
        raise ValueError("idle_window 必须为对象或 null")
    if set(idle_window) != {"start_schedule", "end_schedule"}:
        raise ValueError("idle_window 必须同时包含 start_schedule 和 end_schedule")

    start_schedule = _normalize_idle_window_schedule(
        idle_window["start_schedule"], "start_schedule"
    )
    end_schedule = _normalize_idle_window_schedule(
        idle_window["end_schedule"], "end_schedule"
    )
    if start_schedule["kind"] != end_schedule["kind"]:
        raise ValueError("idle_window 的开始和结束 schedule 必须使用相同 kind")
    if (
        start_schedule["kind"] == "once"
        and datetime.fromisoformat(end_schedule["run_at"]) <= datetime.fromisoformat(start_schedule["run_at"])
    ):
        raise ValueError("idle_window.end_schedule.run_at 必须晚于 start_schedule.run_at")
    return {"start_schedule": start_schedule, "end_schedule": end_schedule}


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, msg, status: int = 400) -> bool:
    bad(handler, msg, status=status)
    return True


def _require_cron_hub_profile(body) -> str:
    if str(body.get("owner_profile") or "").strip():
        raise ValueError("owner_profile is not supported; use profile")
    profile = _normalize_cron_profile_value(body.get("profile"))
    if not profile:
        raise ValueError("profile required")
    return profile


@contextmanager
def _owner_cron_context(owner_profile: str):
    owner = _normalize_profile_name(owner_profile)
    with cron_profile_context_for_home(get_hermes_home_for_profile(owner)):
        yield owner


def try_handle_get(handler, parsed) -> bool:
    if not cron_all_profiles_enabled():
        return False
    if parsed.path == "/api/integration/crons/unread":
        return _handle_unread_summary(handler)
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
    if path == "/api/integration/crons/unread/read":
        return _handle_unread_read(handler, body)
    return False


def _handle_unread_summary(handler):
    try:
        from integration.crons.unread import unread_summary_all_profiles

        return _respond(handler, unread_summary_all_profiles())
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_unread_read(handler, body):
    try:
        require(body, "profile", "job_id")
        from integration.crons.unread import mark_job_read

        return _respond(handler, mark_job_read(body["profile"], body["job_id"]))
    except KeyError as e:
        return _respond_bad(handler, str(e).strip("'") or "Job not found", 404)
    except ValueError as e:
        return _respond_bad(handler, str(e))
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_create(handler, body):
    try:
        require(body, "prompt", "schedule")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
        idle_window = _normalize_idle_window(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import create_job, update_job
    from integration.crons.workspace_policy import normalize_workspace_policy
    from integration.crons.workspace_policy import approved_default_workspace

    toast_notifications = body.get("toast_notifications") is not False
    try:
        with _owner_cron_context(profile) as owner:
            raw_policy = body.get("workspace_policy")
            default_base = (
                approved_default_workspace(get_hermes_home_for_profile(owner))
                if raw_policy is None
                else None
            )
            workspace_policy = normalize_workspace_policy(raw_policy, default_base=default_base)
            job = create_job(
                prompt=body["prompt"],
                schedule=body["schedule"],
                name=body.get("name") or None,
                deliver=body.get("deliver") or "local",
                skills=body.get("skills") or [],
                model=body.get("model") or None,
                workspace_policy=workspace_policy,
            )
            post_create: dict = {"profile": owner, "idle_window": idle_window}
            if not toast_notifications:
                post_create["toast_notifications"] = False
            job = update_job(job["id"], post_create) or job
            return _respond(
                handler,
                {"ok": True, "profile": owner, "job": _cron_job_for_api(job)},
            )
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_update(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
        idle_window = _normalize_idle_window(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import update_job

    updates = {k: v for k, v in body.items() if k in _UPDATE_FIELDS and v is not None}
    if "idle_window" in body:
        updates["idle_window"] = idle_window
    updates["profile"] = profile
    try:
        with _owner_cron_context(profile) as owner:
            if "workspace_policy" in updates:
                from integration.crons.workspace_policy import normalize_workspace_policy

                updates["workspace_policy"] = normalize_workspace_policy(
                    updates["workspace_policy"],
                )
            job = update_job(body["job_id"], updates)
            if not job:
                return _respond_bad(handler, "Job not found", 404)
            return _respond(
                handler,
                {"ok": True, "profile": owner, "job": _cron_job_for_api(job)},
            )
    except Exception as e:
        return _respond(handler, {"error": str(e)}, status=400)


def _handle_delete(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import get_job, remove_job

    with _owner_cron_context(profile):
        job = get_job(body["job_id"])
        if not job:
            return _respond_bad(handler, "Job not found", 404)
    try:
        from integration.crons.session_bridge import delete_cron_job_history

        history_cleanup = delete_cron_job_history(
            body["job_id"],
            owner_profile=profile,
            job=job,
        )
    except Exception as e:
        history_cleanup = {"ok": False, "deleted": False, "error": str(e)}
    if not history_cleanup.get("ok"):
        return _respond(
            handler,
            {"ok": False, "job_id": body["job_id"], "history_cleanup": history_cleanup},
            status=409,
        )
    with _owner_cron_context(profile):
        ok = remove_job(body["job_id"])
    if not ok:
        return _respond_bad(handler, "Job not found", 404)
    return _respond(
        handler,
        {"ok": True, "job_id": body["job_id"], "history_cleanup": history_cleanup},
    )


def _handle_run(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import get_job
    from api.routes import _is_cron_running, _mark_cron_running

    with _owner_cron_context(profile):
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
    storage_home = get_hermes_home_for_profile(profile)
    from integration.crons.execution_model import prepare_cron_hub_execution_job

    job = prepare_cron_hub_execution_job(
        {**job, "profile": profile},
        profile,
        storage_home,
    )
    execution_home = storage_home
    threading.Thread(
        target=_run_cron_tracked,
        args=(job, storage_home, execution_home, profile),
        daemon=True,
    ).start()
    return _respond(handler, {"ok": True, "job_id": body["job_id"], "status": "running"})


def _handle_pause(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import pause_job

    with _owner_cron_context(profile):
        result = pause_job(body["job_id"], reason=body.get("reason"))
    if result:
        return _respond(handler, {"ok": True, "job": _cron_job_for_api(result)})
    return _respond_bad(handler, "Job not found", 404)


def _handle_resume(handler, body):
    try:
        require(body, "job_id")
    except ValueError as e:
        return _respond_bad(handler, str(e))
    try:
        profile = _require_cron_hub_profile(body)
    except ValueError as e:
        return _respond_bad(handler, str(e))

    from cron.jobs import resume_job

    try:
        with _owner_cron_context(profile):
            result = resume_job(body["job_id"])
    except ValueError as exc:
        message = str(exc)
        if (
            message.startswith("Cannot resume: one-shot time ")
            and " is in the past " in message
            and " will never fire." in message
        ):
            message = "执行时间是历史时间，请修改执行时间后启用"
        return _respond_bad(handler, _sanitize_error(message))
    if result:
        return _respond(handler, {"ok": True, "job": _cron_job_for_api(result)})
    return _respond_bad(handler, "Job not found", 404)
