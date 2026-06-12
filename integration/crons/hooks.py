"""Register cron integration hooks (scheduler + manual run materialize)."""

from __future__ import annotations

import copy
import logging

logger = logging.getLogger(__name__)

_installed = False


def materialize_after_cron_run(
    job: dict,
    *,
    owner_profile: str | None = None,
    execution_home=None,
) -> str | None:
    from integration.config import cron_all_profiles_enabled

    if not cron_all_profiles_enabled():
        return None
    from integration.crons.listing import resolve_owner_profile_for_job
    from integration.crons.session_bridge import (
        materialize_cron_session,
        read_cron_output_for_run,
    )

    job_id = str((job or {}).get("id") or "").strip()
    if not job_id:
        return None

    owner = (owner_profile or "").strip() or resolve_owner_profile_for_job(job_id)
    if not owner:
        return None

    if execution_home is None:
        from api.routes import _profile_home_for_cron_job

        execution_home = _profile_home_for_cron_job(job)

    fallback_output, fallback_filename = read_cron_output_for_run(job_id)
    return materialize_cron_session(
        job,
        owner_profile=owner,
        execution_home=execution_home,
        fallback_output=fallback_output,
        fallback_filename=fallback_filename,
    )


def _cron_repeat_limit_will_delete(job: dict) -> bool:
    """Return True when the next mark_job_run would remove this job (repeat exhausted)."""
    repeat = job.get("repeat") if isinstance(job.get("repeat"), dict) else None
    if not repeat:
        return False
    times = repeat.get("times")
    if times is None or times <= 0:
        return False
    completed = repeat.get("completed", 0)
    try:
        completed = int(completed)
    except (TypeError, ValueError):
        completed = 0
    try:
        times = int(times)
    except (TypeError, ValueError):
        return False
    return completed + 1 >= times


def _restore_terminal_once_job(
    job_snapshot: dict,
    *,
    success: bool,
    error: str | None,
    delivery_error: str | None,
    insert_at: int,
) -> None:
    """Re-insert a one-shot job after upstream mark_job_run removed it."""
    from cron.jobs import _hermes_now, load_jobs, save_jobs

    job = copy.deepcopy(job_snapshot)
    now = _hermes_now().isoformat()
    job["last_run_at"] = now
    job["last_status"] = "ok" if success else "error"
    job["last_error"] = error if not success else None
    job["last_delivery_error"] = delivery_error
    if isinstance(job.get("repeat"), dict):
        job["repeat"]["completed"] = job["repeat"].get("completed", 0) + 1
    job["next_run_at"] = None
    job["enabled"] = False
    job["state"] = "completed"

    jobs = load_jobs()
    idx = max(0, min(insert_at, len(jobs)))
    jobs.insert(idx, job)
    save_jobs(jobs)


def _install_preserve_once_cron_hook() -> None:
    """Keep repeat-limited jobs in jobs.json as completed/disabled instead of deleting."""
    try:
        import cron.jobs as _cj
    except ImportError:
        logger.debug("_install_preserve_once_cron_hook: cron.jobs unavailable")
        return

    original = getattr(_cj, "mark_job_run", None)
    if original is None:
        return
    if getattr(original, "_webui_cron_preserve_once_wrapped", False):
        return

    base = getattr(original, "_webui_original_mark_job_run", original)

    def _mark_job_run_preserve_once(
        job_id: str,
        success: bool,
        error=None,
        delivery_error=None,
    ):
        snapshot = None
        insert_at = 0
        will_delete = False
        try:
            from cron.jobs import load_jobs

            jobs = load_jobs()
            for i, job in enumerate(jobs):
                if job.get("id") == job_id:
                    if _cron_repeat_limit_will_delete(job):
                        will_delete = True
                        snapshot = copy.deepcopy(job)
                        insert_at = i
                    break
        except Exception as exc:
            logger.debug(
                "preserve_once_cron: pre-read failed for %s: %s", job_id, exc
            )

        try:
            base(job_id, success, error, delivery_error=delivery_error)
        except TypeError:
            base(job_id, success, error)

        if not will_delete or snapshot is None:
            return

        try:
            from cron.jobs import load_jobs

            still_present = any(
                j.get("id") == job_id for j in load_jobs()
            )
        except Exception:
            still_present = False

        if still_present:
            return

        try:
            _restore_terminal_once_job(
                snapshot,
                success=success,
                error=error,
                delivery_error=delivery_error,
                insert_at=insert_at,
            )
        except Exception as exc:
            logger.warning(
                "preserve_once_cron: failed to restore job %s: %s", job_id, exc
            )

    _mark_job_run_preserve_once._webui_cron_preserve_once_wrapped = True
    _mark_job_run_preserve_once._webui_original_mark_job_run = base
    _cj.mark_job_run = _mark_job_run_preserve_once

    # cron.scheduler imports mark_job_run into its own module namespace. Patch
    # that cached reference too, otherwise scheduled ticks can still call the
    # original function and delete one-shot jobs before WebUI can retain them.
    try:
        import cron.scheduler as _cs

        scheduler_mark = getattr(_cs, "mark_job_run", None)
        if scheduler_mark is original or scheduler_mark is base:
            _cs.mark_job_run = _mark_job_run_preserve_once
    except ImportError:
        pass


def _install_run_job_materialize_hook() -> None:
    """Wrap cron.scheduler.run_job to materialize sessions after each run."""
    try:
        import cron.scheduler as _cs
    except ImportError:
        logger.debug("_install_run_job_materialize_hook: cron.scheduler unavailable")
        return

    original = getattr(_cs, "run_job", None)
    if original is None:
        return
    if getattr(original, "_webui_cron_materialize_wrapped", False):
        return

    from api.profiles import (
        _cron_profile_context_depth,
        _home_for_scheduled_cron_job,
        cron_profile_context_for_home,
    )

    base = getattr(original, "_webui_original_run_job", original)

    def _run_job_with_materialize(job, *args, **kwargs):
        execution_home = _home_for_scheduled_cron_job(job)
        try:
            from integration.crons.listing import resolve_owner_profile_for_job

            owner_profile = resolve_owner_profile_for_job(
                str((job or {}).get("id") or "").strip()
            )
        except Exception:
            owner_profile = None
        if _cron_profile_context_depth() > 0:
            try:
                return base(job, *args, **kwargs)
            finally:
                materialize_after_cron_run(
                    job,
                    owner_profile=owner_profile,
                    execution_home=execution_home,
                )
        try:
            with cron_profile_context_for_home(execution_home):
                return base(job, *args, **kwargs)
        finally:
            materialize_after_cron_run(
                job,
                owner_profile=owner_profile,
                execution_home=execution_home,
            )

    _run_job_with_materialize._webui_cron_materialize_wrapped = True
    _run_job_with_materialize._webui_original_run_job = base
    _cs.run_job = _run_job_with_materialize


def install_cron_integration_hooks() -> None:
    """Patch cron.jobs.mark_job_run and cron.scheduler.run_job for integration."""
    global _installed
    if _installed:
        return
    _installed = True

    from integration.config import cron_all_profiles_enabled

    if not cron_all_profiles_enabled():
        return

    _install_preserve_once_cron_hook()
    _install_run_job_materialize_hook()
