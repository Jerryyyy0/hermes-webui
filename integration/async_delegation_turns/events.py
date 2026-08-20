"""Canonical SSE envelopes for WebUI-owned async delegation state."""

from __future__ import annotations

import time
from typing import Any

from integration.async_delegation_turns.state import _activity_version, _records


_UNSETTLED_WAKEUP_STATES = frozenset({"idle", "queued", "running"})
_EVENT_ID_KINDS = {
    "background_task_dispatched": "dispatched",
    "background_task_status": "status",
    "bg_task_complete": "complete",
    "server_turn_started": "run-started",
    "background_task_unresolved": "unresolved",
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _task_payload(delegation_id: str, record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    child_task_count = max(1, _as_int(record.get("child_task_count"), 1))
    goals = record.get("goals")
    payload = {
        "delegation_id": str(delegation_id),
        "child_task_count": child_task_count,
        "goals": list(goals) if isinstance(goals, list) else [],
        "origin_turn_key": str(record.get("turn_key") or ""),
        "status": str(record.get("status") or "running"),
        "wakeup_state": str(record.get("wakeup_state") or "idle"),
    }
    child_task_summary = record.get("child_task_summary")
    if isinstance(child_task_summary, dict):
        payload["child_task_summary"] = dict(child_task_summary)
    payload.update({key: value for key, value in extra.items() if value is not None})
    cancel_state = str(record.get("cancel_state") or "none")
    if cancel_state != "none":
        payload["cancel_state"] = cancel_state
    return payload


def task_event(
    session: Any,
    event_type: str,
    delegation_id: str,
    record: dict[str, Any] | None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one schema-v1 lifecycle event from a persisted task record."""
    record = dict(record or {})
    version = _as_int(record.get("activity_version"), _activity_version(session))
    event_id = f"{delegation_id}:{_EVENT_ID_KINDS.get(event_type, event_type)}:{version}"
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "session_id": str(getattr(session, "session_id", "") or ""),
        "emitted_at": time.time(),
        "background_activity_version": version,
        "payload": _task_payload(delegation_id, record, **extra),
    }


def snapshot_event(session: Any) -> dict[str, Any]:
    """Return the current unsettled async-delegation state for one subscriber."""
    version = _activity_version(session)
    tasks = []
    for delegation_id, record in _records(session).items():
        status = str(record.get("status") or "running")
        wakeup_state = str(record.get("wakeup_state") or "idle")
        if status != "running" and wakeup_state not in _UNSETTLED_WAKEUP_STATES:
            continue
        tasks.append(
            _task_payload(
                delegation_id,
                record,
                dispatched_at=record.get("created_at"),
                completed_at=record.get("completed_at"),
            )
        )
    return {
        "schema_version": 1,
        "event_id": f"background:snapshot:{version}",
        "event_type": "background_tasks_snapshot",
        "session_id": str(getattr(session, "session_id", "") or ""),
        "emitted_at": time.time(),
        "background_activity_version": version,
        "payload": {
            # ``active_task_count`` is retained for schema-v1 compatibility.
            # It counts delegation batches, not their internal child tasks.
            "active_task_count": len(tasks),
            "active_delegation_count": len(tasks),
            "active_child_task_count": sum(
                _as_int(task.get("child_task_count"), 1) for task in tasks
            ),
            "tasks": tasks,
        },
    }


def unresolved_event(session: Any, delegation_id: str) -> dict[str, Any]:
    """Build the terminal event used when completion ownership is unavailable."""
    version = _activity_version(session)
    return {
        "schema_version": 1,
        "event_id": f"{delegation_id}:unresolved:{version}",
        "event_type": "background_task_unresolved",
        "session_id": str(getattr(session, "session_id", "") or ""),
        "emitted_at": time.time(),
        "background_activity_version": version,
        "payload": {
            "delegation_id": str(delegation_id),
            "reason": "origin_unresolved",
            "retryable": False,
        },
    }


def is_idle(session: Any) -> bool:
    for record in _records(session).values():
        status = str(record.get("status") or "running")
        wakeup_state = str(record.get("wakeup_state") or "idle")
        if status == "running" or wakeup_state in _UNSETTLED_WAKEUP_STATES:
            return False
    return True


def idle_event(session: Any) -> dict[str, Any]:
    version = _activity_version(session)
    now = time.time()
    return {
        "schema_version": 1,
        "event_id": f"background:idle:{version}",
        "event_type": "background_tasks_idle",
        "session_id": str(getattr(session, "session_id", "") or ""),
        "emitted_at": now,
        "background_activity_version": version,
        "payload": {
            "active_task_count": 0,
            "active_delegation_count": 0,
            "active_child_task_count": 0,
            "settled_at": now,
        },
    }
