"""Register cron integration hooks (scheduler + manual run materialize)."""

from __future__ import annotations

import copy
import logging

logger = logging.getLogger(__name__)

_installed = False

_MAX_ITERATION_SUMMARY_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools."
)


def _normalized_message_text(message: dict) -> str:
    return " ".join(str(message.get("content") or "").split())


def _is_max_iteration_summary_request(messages: list, index: int) -> bool:
    """Recognize the exact internal summary request in a cron execution trace."""
    if index <= 0 or index >= len(messages):
        return False
    message = messages[index]
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if _normalized_message_text(message) != _MAX_ITERATION_SUMMARY_REQUEST:
        return False

    previous_user_index = next(
        (
            candidate
            for candidate in range(index - 1, -1, -1)
            if isinstance(messages[candidate], dict)
            and messages[candidate].get("role") == "user"
        ),
        None,
    )
    if previous_user_index is None:
        return False

    has_tool_activity = any(
        isinstance(candidate, dict)
        and (
            candidate.get("role") == "tool"
            or (
                candidate.get("role") == "assistant"
                and bool(candidate.get("tool_calls") or candidate.get("_partial_tool_calls"))
            )
        )
        for candidate in messages[previous_user_index + 1:index]
    )
    if not has_tool_activity:
        return False

    return any(
        isinstance(candidate, dict) and candidate.get("role") == "assistant"
        for candidate in messages[index + 1:]
    )


def normalize_cron_manifest_messages(
    messages: list,
    *,
    require_stable_real_turn: bool = False,
) -> list:
    """Return the cron transcript view used by manifest persistence and GET.

    Hermes Agent's max-iteration fallback is persisted as a user-role request.
    It drives a final summary but is not a new cron invocation, so it must not
    split the manifest turn. Historical transcripts are left untouched on GET:
    the read path opts in only when a real user row already has a stable key and
    recognized internal rows have never been assigned one.
    """
    source = list(messages or [])
    internal_indexes = {
        index
        for index in range(len(source))
        if _is_max_iteration_summary_request(source, index)
    }
    if not internal_indexes:
        return source

    if require_stable_real_turn:
        stable_real_turn = any(
            isinstance(message, dict)
            and message.get("role") == "user"
            and index not in internal_indexes
            and str(message.get("_turn_key") or "").strip()
            for index, message in enumerate(source)
        )
        internal_was_stamped = any(
            str(source[index].get("_turn_key") or "").strip()
            for index in internal_indexes
            if isinstance(source[index], dict)
        )
        if not stable_real_turn or internal_was_stamped:
            return source

    return [
        message
        for index, message in enumerate(source)
        if index not in internal_indexes
    ]


def _stamp_cron_manifest_turn_keys(messages: list) -> list:
    """Stamp missing real cron user rows with stable sequential turn keys."""
    stamped = list(messages or [])
    max_turn = 0
    for message in stamped:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        key = str(message.get("_turn_key") or "").strip()
        if not key.startswith("turn:"):
            continue
        try:
            max_turn = max(max_turn, int(key.split(":", 1)[1]))
        except (TypeError, ValueError):
            continue

    next_turn = max_turn + 1
    for message in stamped:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if str(message.get("_turn_key") or "").strip():
            continue
        message["_turn_key"] = f"turn:{next_turn}"
        next_turn += 1
    return stamped


def materialize_after_cron_run(
    job: dict,
    *,
    owner_profile: str | None = None,
    execution_home=None,
) -> str | None:
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
    sid = materialize_cron_session(
        job,
        owner_profile=owner,
        execution_home=execution_home,
        fallback_output=fallback_output,
        fallback_filename=fallback_filename,
    )
    # After materializing the sidecar, persist turn_artifacts so the manifest
    # can read from the store/table instead of re-extracting every time and
    # survives context compression. Mirrors what _run_agent_streaming does at
    # api/streaming.py:7043-7046 for WebUI-origin sessions.
    if sid:
        _persist_cron_turn_artifacts(sid)
    return sid


def _persist_cron_turn_artifacts(sid: str) -> None:
    """Persist manifest artifact records for a materialized cron session.

    Cron sessions originate from hermes-agent's scheduler, which does not call
    _persist_turn_artifact_paths (that's a WebUI streaming-pipeline function).
    Without this step, cron session manifests rely entirely on real-time
    extraction from messages, which is lost after context compression.

    This function loads the materialized sidecar, stamps _turn_key on user
    messages that lack one, then calls _persist_turn_artifact_paths for each
    turn so session_manifest_records receives stable turn decisions. The sidecar
    JSON is still saved for _turn_key stamps, not for artifact ownership.
    """
    from api.models import Session

    s = Session.load(sid)
    if s is None or not getattr(s, "messages", None):
        return

    from api.session_manifest import _message_turns
    from api.streaming import _persist_turn_artifact_paths

    s.messages = _stamp_cron_manifest_turn_keys(
        normalize_cron_manifest_messages(s.messages)
    )

    # Match the normal WebUI durability order: stable transcript keys must be
    # durable before the manifest store accepts artifact or empty decisions.
    try:
        s.save()
    except Exception:
        logger.debug("Failed to save cron session %s before artifact persist", sid, exc_info=True)
        return

    for turn in _message_turns(s.messages):
        tk = str(turn.get("turn_key") or "").strip()
        if tk:
            _persist_turn_artifact_paths(s, tk)


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
    """Patch cron.jobs.mark_job_run and cron.scheduler.run_job for integration.

    The materialize hook (session import + turn_artifacts persistence) is
    installed unconditionally — it is a read-only import from state.db into
    the WebUI sidecar and has no dependency on cross-profile integration
    features. Without it, cron sessions never get a sidecar, so
    /api/session/manifest returns 404 and turn_artifacts are never persisted.

    The preserve-once hook (keeping repeat-limited jobs in jobs.json as
    completed/disabled) remains gated on cron_all_profiles_enabled() because
    it alters cron job lifecycle behavior that only matters when the Cron Hub
    UI is active.
    """
    global _installed
    if _installed:
        return
    _installed = True

    # Always install the materialize hook — cron sessions need sidecars +
    # turn_artifacts persistence regardless of integration mode.
    _install_run_job_materialize_hook()

    from integration.config import cron_all_profiles_enabled

    if not cron_all_profiles_enabled():
        return

    _install_preserve_once_cron_hook()
