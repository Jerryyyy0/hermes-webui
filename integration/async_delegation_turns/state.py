"""Persist and resolve async-delegation ownership in the WebUI sidecar."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from integration.agent_message_semantics.classifier import is_non_anchor_control_message

logger = logging.getLogger(__name__)

_UNSETTLED_WAKEUP_STATES = frozenset({"idle", "queued", "running"})
_WAKEUP_ACK_PENDING = "agent_ack_pending"
_WAKEUP_ACK_UNKNOWN = "agent_ack_unknown"
_WAKEUP_MAX_START_ATTEMPTS = 5


def normalize_async_delegation_status(value: Any) -> str:
    """Map Agent terminal spellings onto the WebUI lifecycle contract."""
    status = str(value or "").strip().lower()
    if not status:
        return "completed"
    if status in {"completed", "success", "succeeded"}:
        return "completed"
    if status in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    if status == "running":
        return "running"
    return "failed"


def _activity_version(session: Any) -> int:
    try:
        return max(0, int(getattr(session, "async_delegation_activity_version", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _bump_activity_version(session: Any) -> int:
    version = _activity_version(session) + 1
    session.async_delegation_activity_version = version
    return version


def _records(session: Any) -> dict[str, dict[str, Any]]:
    raw = getattr(session, "async_delegation_origins", None)
    if not isinstance(raw, dict):
        raw = {}
    records: dict[str, dict[str, Any]] = {}
    for delegation_id, record in raw.items():
        if isinstance(record, dict):
            records[str(delegation_id)] = dict(record)
    return records


def _save(session: Any) -> None:
    # Background lifecycle changes must not reorder a session in the sidebar.
    session.save(touch_updated_at=False)


def _message_item(record: dict[str, Any], *, goals: list[str] | None = None) -> dict[str, Any]:
    """Return the stable, public lifecycle projection for one delegation."""
    try:
        child_task_count = max(1, int(record.get("child_task_count") or 1))
    except (TypeError, ValueError):
        child_task_count = 1
    item = {
        "status": str(record.get("status") or "running"),
        "wakeup_state": str(record.get("wakeup_state") or "idle"),
        "cancel_state": str(record.get("cancel_state") or "none"),
        "child_task_count": child_task_count,
        "goals": list(goals or []),
        "dispatched_at": record.get("dispatched_at") or record.get("created_at"),
        "completed_at": record.get("completed_at"),
    }
    if isinstance(record.get("child_task_summary"), dict):
        item["child_task_summary"] = dict(record["child_task_summary"])
    return item


def _turn_state(items: list[dict[str, Any]]) -> str:
    if not items:
        return "settled"
    if any(
        item["status"] == "running" or item["wakeup_state"] in _UNSETTLED_WAKEUP_STATES
        for item in items
    ):
        return "cancelling" if any(item["cancel_state"] == "requested" for item in items) else "running"
    if all(item["status"] == "cancelled" for item in items):
        return "cancelled"
    return "settled"


def _existing_item_goals(message: dict[str, Any], delegation_id: str) -> list[str]:
    """Keep presentation-only goals on the origin user message."""
    lifecycle = message.get("async_delegations")
    items = lifecycle.get("items") if isinstance(lifecycle, dict) else None
    item = items.get(delegation_id) if isinstance(items, dict) else None
    goals = item.get("goals") if isinstance(item, dict) else None
    return [str(goal) for goal in goals if isinstance(goal, str) and goal] if isinstance(goals, list) else []


def _project_turns(
    session: Any,
    records: dict[str, dict[str, Any]],
    *,
    dispatch_goals: dict[str, list[str]] | None = None,
) -> None:
    """Project delegation state onto the exact origin user message only."""
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return
    by_turn: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for delegation_id, record in records.items():
        turn_key = str(record.get("turn_key") or "").strip()
        if turn_key:
            by_turn.setdefault(turn_key, []).append((delegation_id, record))
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if is_non_anchor_control_message(message):
            continue
        turn_key = str(message.get("_turn_key") or "").strip()
        records_for_turn = by_turn.get(turn_key)
        if not records_for_turn:
            continue
        # ``async_delegations.items`` owns the public delegation IDs. Remove
        # the legacy duplicate whenever this origin turn is persisted again.
        message.pop("_background_task_ids", None)
        items = {}
        for delegation_id, record in records_for_turn:
            goals = _existing_item_goals(message, delegation_id)
            if not goals and dispatch_goals:
                goals = list(dispatch_goals.get(delegation_id) or [])
            # Read old sidecars once during migration, then persist goals only
            # on the origin user message below.
            if not goals and isinstance(record.get("goals"), list):
                goals = [str(goal) for goal in record["goals"] if isinstance(goal, str) and goal]
            items[delegation_id] = _message_item(record, goals=goals)
        message["async_delegations"] = {
            "state": _turn_state(list(items.values())),
            "items": items,
        }


def _canonical_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return the compact, runtime-only top-level sidecar record."""
    canonical = dict(record)
    canonical.pop("delegation_id", None)  # the mapping key is the canonical ID
    canonical.pop("goals", None)  # presentation data belongs to the user turn
    if canonical.get("dispatched_at") is None and canonical.get("created_at") is not None:
        canonical["dispatched_at"] = canonical["created_at"]
    canonical.pop("created_at", None)
    if canonical.get("wakeup") == {}:
        canonical.pop("wakeup", None)
    return canonical


def _persist(
    session: Any,
    records: dict[str, dict[str, Any]],
    *,
    dispatch_goals: dict[str, list[str]] | None = None,
) -> None:
    _project_turns(session, records, dispatch_goals=dispatch_goals)
    session.async_delegation_origins = {
        delegation_id: _canonical_record(record)
        for delegation_id, record in records.items()
    }
    _save(session)


def _apply_records(
    session: Any,
    records: dict[str, dict[str, Any]],
    *,
    dispatch_goals: dict[str, list[str]] | None = None,
) -> None:
    """Apply sidecar records without saving.

    Start admission uses this helper so the wakeup lifecycle and the core
    pending fields can be committed by one caller-owned ``Session.save()``.
    All other mutations should use ``_persist``.
    """
    _project_turns(session, records, dispatch_goals=dispatch_goals)
    session.async_delegation_origins = {
        delegation_id: _canonical_record(record)
        for delegation_id, record in records.items()
    }


def _wakeup(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("wakeup")
    return dict(value) if isinstance(value, dict) else {}


def receive_completion(
    session: Any,
    delegation_id: str,
    *,
    prompt: str,
    status: str = "completed",
    child_task_summary: dict[str, int] | None = None,
    ack_pending: bool = True,
) -> dict[str, Any] | None:
    """Persist a completion as a durable WebUI inbox item.

    This is deliberately separate from ``mark_async_delegation_completion``:
    the latter is retained for legacy callers that already own the wakeup
    payload. New delivery writes the prompt before acknowledging Agent.
    """
    delegation_id = str(delegation_id or "").strip()
    prompt = str(prompt or "").strip()
    if not delegation_id or not prompt:
        return None
    records = _records(session)
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    current_wakeup = _wakeup(record)
    existing_prompt = str(current_wakeup.get("prompt") or "")
    if existing_prompt and existing_prompt != prompt:
        record["status"] = "failed"
        record["wakeup_state"] = "failed"
        current_wakeup["error_code"] = "completion_payload_conflict"
        record["wakeup"] = current_wakeup
        record["activity_version"] = _bump_activity_version(session)
        records[delegation_id] = record
        _persist(session, records)
        logger.error(
            "async_delegation_payload_conflict session_id=%s delegation_id=%s",
            getattr(session, "session_id", ""),
            delegation_id,
        )
        return dict(record)
    # A terminal record is an idempotency fence.  Agent may replay the same
    # completion after an ACK/restart; never resurrect a settled/failed wakeup
    # into the executable queue.
    if str(record.get("wakeup_state") or "") in {"settled", "failed"}:
        return dict(record)
    if str(record.get("cancel_state") or "none") in {"requested", "cancelled"}:
        record["status"] = normalize_async_delegation_status(status)
        record["wakeup_state"] = "settled"
        record["cancel_state"] = "cancelled"
        record["completed_at"] = record.get("completed_at") or time.time()
        if child_task_summary is not None:
            record["child_task_summary"] = dict(child_task_summary)
        records[delegation_id] = record
        _persist(session, records)
        return dict(record)
    record["status"] = normalize_async_delegation_status(status)
    record["completed_at"] = record.get("completed_at") or time.time()
    record["wakeup_state"] = "queued"
    current_wakeup.setdefault("stream_id", None)
    current_wakeup.setdefault("start_attempts", 0)
    current_wakeup["prompt"] = existing_prompt or prompt
    current_wakeup["error_code"] = _WAKEUP_ACK_PENDING if ack_pending else _WAKEUP_ACK_UNKNOWN
    record["wakeup"] = current_wakeup
    if child_task_summary is not None:
        record["child_task_summary"] = dict(child_task_summary)
    record["activity_version"] = _bump_activity_version(session)
    records[delegation_id] = record
    _persist(session, records)
    logger.info(
        "async_delegation_inbox_received session_id=%s delegation_id=%s wakeup_state=%s",
        getattr(session, "session_id", ""),
        delegation_id,
        record.get("wakeup_state"),
    )
    return dict(record)


def mark_completion_ack(
    session: Any,
    delegation_id: str,
    *,
    acknowledged: bool,
    unknown: bool = False,
) -> dict[str, Any] | None:
    """Persist the Agent ACK/readback result without changing queue state."""
    delegation_id = str(delegation_id or "").strip()
    records = _records(session)
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    wakeup = _wakeup(record)
    wakeup["error_code"] = _WAKEUP_ACK_UNKNOWN if unknown else (None if acknowledged else _WAKEUP_ACK_PENDING)
    record["wakeup"] = wakeup
    records[delegation_id] = record
    _persist(session, records)
    logger.info(
        "async_delegation_agent_ack_%s session_id=%s delegation_id=%s",
        "unknown" if unknown else "acknowledged" if acknowledged else "failed",
        getattr(session, "session_id", ""),
        delegation_id,
    )
    return dict(record)


def select_next_queued_wakeup(session: Any) -> tuple[str, dict[str, Any]] | None:
    """Return the oldest executable queued item without mutating the session."""
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for delegation_id, raw in _records(session).items():
        record = dict(raw)
        if str(record.get("wakeup_state") or "idle") != "queued":
            continue
        if str(record.get("cancel_state") or "none") != "none":
            continue
        wakeup = _wakeup(record)
        if not str(wakeup.get("prompt") or "").strip():
            continue
        if wakeup.get("error_code") == _WAKEUP_ACK_PENDING:
            continue
        try:
            completed_at = float(record.get("completed_at") or 0)
        except (TypeError, ValueError):
            completed_at = 0.0
        candidates.append((completed_at, delegation_id, record))
    if not candidates:
        return None
    _, delegation_id, record = min(candidates, key=lambda item: (item[0], item[1]))
    return delegation_id, record


def validate_wakeup_start_locked(
    session: Any,
    delegation_id: str,
    *,
    turn_key: str,
) -> dict[str, Any] | None:
    """Validate an admission without mutating the cached Session object."""
    record = _records(session).get(str(delegation_id or "").strip())
    if not isinstance(record, dict):
        return None
    record = dict(record)
    if str(record.get("wakeup_state") or "idle") != "queued":
        return None
    if str(record.get("cancel_state") or "none") != "none":
        return None
    if str(record.get("turn_key") or "").strip() != str(turn_key or "").strip():
        return None
    wakeup = _wakeup(record)
    if not str(wakeup.get("prompt") or "").strip() or wakeup.get("error_code") == _WAKEUP_ACK_PENDING:
        return None
    return record


def prepare_wakeup_start_locked(
    session: Any,
    delegation_id: str,
    *,
    stream_id: str,
    turn_key: str,
    generation: int,
) -> dict[str, Any] | None:
    """Mutate wakeup admission state; caller must perform the one Session.save()."""
    delegation_id = str(delegation_id or "").strip()
    records = _records(session)
    record = validate_wakeup_start_locked(session, delegation_id, turn_key=turn_key)
    if record is None:
        return None
    wakeup = _wakeup(record)
    wakeup["stream_id"] = str(stream_id)
    wakeup["start_attempts"] = max(0, int(wakeup.get("start_attempts") or 0)) + 1
    wakeup["error_code"] = None
    record["wakeup"] = wakeup
    record["wakeup_state"] = "running"
    record["activity_version"] = _bump_activity_version(session)
    records[delegation_id] = record
    _apply_records(session, records)
    logger.info(
        "async_delegation_wakeup_admitted session_id=%s delegation_id=%s stream_id=%s generation=%s",
        getattr(session, "session_id", ""),
        delegation_id,
        stream_id,
        generation,
    )
    return dict(record)


def requeue_unstarted_wakeup(
    session: Any,
    delegation_id: str,
    *,
    stream_id: str,
    generation: int,
    error_code: str,
    save: bool = True,
) -> dict[str, Any] | None:
    """Revert only the exact admission that failed before worker dispatch."""
    records = _records(session)
    delegation_id = str(delegation_id or "").strip()
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    wakeup = _wakeup(record)
    if (
        str(record.get("wakeup_state") or "") != "running"
        or str(wakeup.get("stream_id") or "") != str(stream_id)
        or getattr(session, "active_stream_generation", None) != generation
    ):
        return None
    wakeup["stream_id"] = None
    attempts = max(0, int(wakeup.get("start_attempts") or 0))
    exhausted = attempts >= _WAKEUP_MAX_START_ATTEMPTS
    wakeup["error_code"] = (
        "start_retry_exhausted" if exhausted else str(error_code or "start_failed")
    )
    record["wakeup"] = wakeup
    record["wakeup_state"] = "failed" if exhausted else "queued"
    if exhausted:
        record["status"] = "failed"
    record["activity_version"] = _bump_activity_version(session)
    records[delegation_id] = record
    if save:
        _persist(session, records)
    else:
        _apply_records(session, records)
    return dict(record)


def record_retryable_wakeup_start_failure(
    session: Any,
    delegation_id: str,
    *,
    previous_attempts: int,
    error_code: str,
) -> dict[str, Any] | None:
    """Count one retryable pre-dispatch failure without double-counting rollback."""
    records = _records(session)
    delegation_id = str(delegation_id or "").strip()
    record = records.get(delegation_id)
    if not isinstance(record, dict) or str(record.get("wakeup_state") or "") != "queued":
        return None
    record = dict(record)
    wakeup = _wakeup(record)
    current_attempts = max(0, int(wakeup.get("start_attempts") or 0))
    attempts = max(current_attempts, max(0, int(previous_attempts)) + 1)
    exhausted = attempts >= _WAKEUP_MAX_START_ATTEMPTS
    wakeup["start_attempts"] = attempts
    wakeup["error_code"] = "start_retry_exhausted" if exhausted else str(error_code)
    record["wakeup"] = wakeup
    record["wakeup_state"] = "failed" if exhausted else "queued"
    if exhausted:
        record["status"] = "failed"
    record["activity_version"] = _bump_activity_version(session)
    records[delegation_id] = record
    _persist(session, records)
    return dict(record)


def settle_wakeup(
    session: Any,
    delegation_id: str,
    *,
    stream_id: str | None,
    generation: int | None,
    status: str = "completed",
    cancel_state: str | None = None,
    error_code: str | None = None,
    clear_prompt: bool = True,
) -> dict[str, Any] | None:
    """Conditionally settle one wakeup after durable output finalization."""
    records = _records(session)
    delegation_id = str(delegation_id or "").strip()
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    wakeup = _wakeup(record)
    expected_stream = str(wakeup.get("stream_id") or "").strip()
    if stream_id and expected_stream and expected_stream != str(stream_id):
        return None
    if generation is not None and getattr(session, "active_stream_generation", None) not in (None, generation):
        return None
    normalized = normalize_async_delegation_status(status)
    record["status"] = normalized
    record["wakeup_state"] = "failed" if error_code else "settled"
    if cancel_state is not None:
        record["cancel_state"] = str(cancel_state)
    if error_code:
        wakeup["error_code"] = str(error_code)
    if clear_prompt and record["wakeup_state"] == "settled":
        records[delegation_id] = {**record, "wakeup": {}}
        # The wakeup owns the core pending projection for this logical turn.
        # Clear it together with the settled sidecar so a completed run cannot
        # leave stale async source/turn metadata behind on the next read.
        if (
            str(getattr(session, "pending_user_source", "") or "")
            == "async_delegation_wakeup"
            and str(getattr(session, "pending_turn_key", "") or "")
            == str(record.get("turn_key") or "")
        ):
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            session.pending_turn_key = None
            session.pending_user_visible = False
    else:
        records[delegation_id] = {**record, "wakeup": wakeup}
    _refresh_cancellation(session, records)
    _persist(session, records)
    logger.info(
        "async_delegation_wakeup_%s session_id=%s delegation_id=%s stream_id=%s",
        record["wakeup_state"],
        getattr(session, "session_id", ""),
        delegation_id,
        stream_id or "",
    )
    return dict(records[delegation_id])


def _refresh_cancellation(session: Any, records: dict[str, dict[str, Any]]) -> None:
    cancellation = getattr(session, "async_delegation_cancellation", None)
    if not isinstance(cancellation, dict) or cancellation.get("state") != "cancelling":
        return
    delegation_ids = [str(item) for item in cancellation.get("delegation_ids") or []]
    if not delegation_ids:
        return
    if any(
        (records.get(delegation_id) or {}).get("status") == "running"
        or (records.get(delegation_id) or {}).get("wakeup_state") in _UNSETTLED_WAKEUP_STATES
        for delegation_id in delegation_ids
    ):
        return
    settled_at = time.time()
    session.async_delegation_cancellation = {
        **cancellation,
        "state": "settled",
        "settled_at": settled_at,
    }


def _idle_cancellation_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "delegation_ids": [],
        "requested_at": None,
        "settled_at": None,
    }


def cancellation_status(session: Any) -> dict[str, Any]:
    cancellation = getattr(session, "async_delegation_cancellation", None)
    if not isinstance(cancellation, dict) or cancellation.get("state") not in {"cancelling", "settled"}:
        return _idle_cancellation_status()
    return {
        "state": str(cancellation.get("state")),
        "delegation_ids": [str(item) for item in cancellation.get("delegation_ids") or []],
        "requested_at": cancellation.get("requested_at"),
        "settled_at": cancellation.get("settled_at"),
    }


def begin_async_delegation_cancellation(session: Any, *, now: float | None = None) -> dict[str, Any]:
    """Atomically snapshot the currently unsettled delegation IDs."""
    current = cancellation_status(session)
    if current["state"] == "cancelling":
        return current
    records = _records(session)
    delegation_ids = [
        delegation_id
        for delegation_id, record in records.items()
        if str(record.get("status") or "running") == "running"
        or str(record.get("wakeup_state") or "idle") in _UNSETTLED_WAKEUP_STATES
    ]
    if not delegation_ids:
        # Keep the last settled barrier for GET polling, but a new POST with
        # no work is an idle request rather than a replay of that old result.
        return _idle_cancellation_status()
    requested_at = time.time() if now is None else float(now)
    session.async_delegation_cancellation = {
        "state": "cancelling",
        "delegation_ids": delegation_ids,
        "requested_at": requested_at,
        "settled_at": None,
    }
    for delegation_id in delegation_ids:
        record = dict(records[delegation_id])
        record["cancel_state"] = "requested"
        records[delegation_id] = record
    _persist(session, records)
    return cancellation_status(session)


def cancellation_delegation_ids(session: Any) -> set[str]:
    cancellation = getattr(session, "async_delegation_cancellation", None)
    if not isinstance(cancellation, dict) or cancellation.get("state") != "cancelling":
        return set()
    return {str(item) for item in cancellation.get("delegation_ids") or []}


def running_wakeup_stream_ids(session: Any, delegation_ids: set[str] | None = None) -> list[str]:
    """Return exact parent wakeup streams covered by a cancellation barrier."""
    wanted = delegation_ids if delegation_ids is not None else set(_records(session))
    stream_ids: list[str] = []
    for delegation_id, record in _records(session).items():
        if delegation_id not in wanted or str(record.get("wakeup_state") or "") != "running":
            continue
        wakeup = _wakeup(record)
        stream_id = str(wakeup.get("stream_id") or "").strip()
        if stream_id and stream_id not in stream_ids:
            stream_ids.append(stream_id)
    return stream_ids


def mark_async_delegation_unresolved(session: Any, delegation_id: str) -> None:
    """Remove an unresolved completion from cancellation/activity accounting."""
    records = _records(session)
    record = records.get(str(delegation_id or "").strip())
    if isinstance(record, dict):
        record = dict(record)
        record["status"] = "failed"
        record["wakeup_state"] = "settled"
        record["completed_at"] = record.get("completed_at") or time.time()
        record["activity_version"] = _bump_activity_version(session)
        records[str(delegation_id).strip()] = record
        _refresh_cancellation(session, records)
        _persist(session, records)


def _dispatch_metadata(payload: dict[str, Any]) -> tuple[int, list[str]]:
    raw_goals = payload.get("goals")
    goals = (
        [str(goal) for goal in raw_goals if isinstance(goal, str) and goal]
        if isinstance(raw_goals, list)
        else []
    )
    try:
        child_task_count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        child_task_count = 0
    if goals:
        child_task_count = len(goals)
    child_task_count = max(1, child_task_count)
    return child_task_count, goals


def record_async_delegation_dispatch(
    session: Any,
    function_result: Any,
    *,
    turn_key: str,
) -> dict[str, Any] | None:
    """Record a successful background ``delegate_task`` against its source turn."""
    payload = function_result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "dispatched" or payload.get("mode") != "background":
        return None
    delegation_id = str(payload.get("delegation_id") or "").strip()
    origin_turn_key = str(turn_key or "").strip()
    if not delegation_id or not origin_turn_key:
        return None
    child_task_count, goals = _dispatch_metadata(payload)

    records = _records(session)
    record = dict(records.get(delegation_id) or {})
    existing = bool(record)
    if not existing:
        record.update(
            {
                "turn_key": origin_turn_key,
                "dispatched_at": time.time(),
                "status": "running",
                "wakeup_state": "idle",
                "completed_at": None,
                "cancel_state": "none",
                "child_task_count": child_task_count,
            }
        )
    else:
        record.setdefault("cancel_state", "none")
        record.setdefault("child_task_count", child_task_count)
    record["activity_version"] = (
        int(record.get("activity_version") or _activity_version(session))
        if existing
        else _bump_activity_version(session)
    )
    records[delegation_id] = record
    _persist(
        session,
        records,
        dispatch_goals={delegation_id: goals} if not existing else None,
    )
    logger.debug(
        "hermes_message_semantics action=async_delegation_origin_recorded "
        "class=context_anchor kind=async_delegation_completion role=user "
        "session_id=%s turn_key_present=%s delegation_id=%s",
        getattr(session, "session_id", ""),
        bool(origin_turn_key),
        delegation_id,
    )
    # The transient return value identifies the event being emitted; the
    # persisted map deliberately keeps that ID only as its key.
    return {
        "delegation_id": delegation_id,
        **dict(session.async_delegation_origins[delegation_id]),
    }


def resolve_async_delegation_origin(session: Any, delegation_id: str) -> dict[str, Any] | None:
    """Return a validated sidecar origin record; never infer from transcript order."""
    record = _records(session).get(str(delegation_id or "").strip())
    if not isinstance(record, dict) or not str(record.get("turn_key") or "").strip():
        return None
    return dict(record)


def mark_async_delegation_completion(
    session: Any,
    delegation_id: str,
    *,
    wakeup_state: str,
    content: Any,
    status: str = "completed",
    child_task_summary: dict[str, int] | None = None,
    cancel_state: str | None = None,
) -> dict[str, Any] | None:
    """Persist completion receipt without materializing a transcript message."""
    delegation_id = str(delegation_id or "").strip()
    records = _records(session)
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    was_status = record.get("status")
    was_wakeup_state = record.get("wakeup_state")
    normalized_status = normalize_async_delegation_status(status)
    record["status"] = normalized_status
    record["completed_at"] = record.get("completed_at") or time.time()
    if cancel_state is not None:
        record["cancel_state"] = str(cancel_state)
    if child_task_summary is not None:
        record["child_task_summary"] = dict(child_task_summary)
    changed = (
        was_status != normalized_status or was_wakeup_state != wakeup_state
    )
    record["wakeup_state"] = wakeup_state
    if changed:
        record["activity_version"] = _bump_activity_version(session)
    else:
        record["activity_version"] = int(record.get("activity_version") or _activity_version(session))
    records[delegation_id] = record
    _refresh_cancellation(session, records)
    _persist(session, records)
    logger.debug(
        "hermes_message_semantics action=background_task_status "
        "class=context_anchor kind=async_delegation_completion role=user "
        "session_id=%s turn_key_present=%s delegation_id=%s wakeup_state=%s",
        getattr(session, "session_id", ""),
        bool(record.get("turn_key")),
        delegation_id,
        wakeup_state,
    )
    return dict(record)


def mark_async_delegation_wakeup(
    session: Any,
    delegation_id: str,
    *,
    wakeup_state: str,
    content: Any,
    error_code: str | None = None,
) -> dict[str, Any] | None:
    """Advance the parent Agent continuation lifecycle for one delegation."""
    delegation_id = str(delegation_id or "").strip()
    records = _records(session)
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    changed = record.get("wakeup_state") != wakeup_state
    record["wakeup_state"] = wakeup_state
    if error_code is not None:
        wakeup = _wakeup(record)
        wakeup["error_code"] = str(error_code)
        record["wakeup"] = wakeup
    if changed:
        record["activity_version"] = _bump_activity_version(session)
    else:
        record["activity_version"] = int(record.get("activity_version") or _activity_version(session))
    records[delegation_id] = record
    _refresh_cancellation(session, records)
    _persist(session, records)
    logger.debug(
        "hermes_message_semantics action=background_task_status "
        "class=context_anchor kind=async_delegation_completion role=user "
        "session_id=%s turn_key_present=%s delegation_id=%s wakeup_state=%s",
        getattr(session, "session_id", ""),
        bool(record.get("turn_key")),
        delegation_id,
        wakeup_state,
    )
    return dict(record)
