"""Persist and resolve async-delegation ownership in the WebUI sidecar."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_UNSETTLED_WAKEUP_STATES = frozenset({"idle", "queued", "running"})


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
