"""Register cron integration hooks (scheduler + manual run materialize)."""

from __future__ import annotations

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


def install_cron_integration_hooks() -> None:
    """Patch scheduler run_job finally and routes _run_cron_tracked (via routes seam)."""
    global _installed
    if _installed:
        return
    _installed = True

    from integration.config import cron_all_profiles_enabled

    if not cron_all_profiles_enabled():
        return

    try:
        import cron.scheduler as _cs
    except ImportError:
        logger.debug("install_cron_integration_hooks: cron.scheduler unavailable")
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
        if _cron_profile_context_depth() > 0:
            try:
                return base(job, *args, **kwargs)
            finally:
                materialize_after_cron_run(
                    job,
                    execution_home=execution_home,
                )
        try:
            with cron_profile_context_for_home(execution_home):
                return base(job, *args, **kwargs)
        finally:
            materialize_after_cron_run(
                job,
                execution_home=execution_home,
            )

    _run_job_with_materialize._webui_cron_materialize_wrapped = True
    _run_job_with_materialize._webui_original_run_job = base
    _cs.run_job = _run_job_with_materialize
