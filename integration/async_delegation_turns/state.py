"""Persist and resolve async-delegation ownership in the WebUI sidecar."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


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


def _dispatch_metadata(payload: dict[str, Any]) -> tuple[str, int, list[str]]:
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
    return ("batch" if child_task_count > 1 else "single", child_task_count, goals)


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
    delegation_kind, child_task_count, goals = _dispatch_metadata(payload)

    records = _records(session)
    record = dict(records.get(delegation_id) or {})
    existing = bool(record)
    if not existing:
        record.update(
            {
                "delegation_id": delegation_id,
                "turn_key": origin_turn_key,
                "created_at": time.time(),
                "status": "running",
                "wakeup_state": "idle",
                "completed_at": None,
                "delegation_kind": delegation_kind,
                "child_task_count": child_task_count,
                "goals": goals,
            }
        )
    else:
        record["delegation_id"] = delegation_id
        record.setdefault("delegation_kind", delegation_kind)
        record.setdefault("child_task_count", child_task_count)
        if not record.get("goals") and goals:
            record["goals"] = goals
    record["activity_version"] = (
        int(record.get("activity_version") or _activity_version(session))
        if existing
        else _bump_activity_version(session)
    )
    records[delegation_id] = record
    session.async_delegation_origins = records
    _save(session)
    logger.debug(
        "hermes_message_semantics action=async_delegation_origin_recorded "
        "class=context_anchor kind=async_delegation_completion role=user "
        "session_id=%s turn_key_present=%s delegation_id=%s",
        getattr(session, "session_id", ""),
        bool(origin_turn_key),
        delegation_id,
    )
    return dict(record)


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
    session.async_delegation_origins = records
    _save(session)
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
    session.async_delegation_origins = records
    _save(session)
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
