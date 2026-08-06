"""Persist and resolve async-delegation ownership in the WebUI sidecar."""

from __future__ import annotations

import json
import logging
import time
from typing import Any


logger = logging.getLogger(__name__)


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

    records = _records(session)
    record = dict(records.get(delegation_id) or {})
    record.update(
        {
            "turn_key": origin_turn_key,
            "created_at": record.get("created_at") or time.time(),
            "status": "running",
            "wakeup_state": "idle",
            "completed_at": None,
        }
    )
    records[delegation_id] = record
    session.async_delegation_origins = records
    _save(session)
    logger.info(
        "hermes_message_semantics action=async_delegation_origin_recorded "
        "class=context_anchor kind=async_delegation_completion role=user "
        "content=%r session_id=%s turn_key=%s delegation_id=%s",
        function_result,
        getattr(session, "session_id", ""),
        origin_turn_key,
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
) -> dict[str, Any] | None:
    """Persist completion receipt without materializing a transcript message."""
    delegation_id = str(delegation_id or "").strip()
    records = _records(session)
    record = records.get(delegation_id)
    if not isinstance(record, dict):
        return None
    record = dict(record)
    record["status"] = "completed"
    record["completed_at"] = record.get("completed_at") or time.time()
    record["wakeup_state"] = wakeup_state
    records[delegation_id] = record
    session.async_delegation_origins = records
    _save(session)
    logger.info(
        "hermes_message_semantics action=background_task_status "
        "class=context_anchor kind=async_delegation_completion role=user "
        "content=%r session_id=%s turn_key=%s delegation_id=%s wakeup_state=%s",
        content,
        getattr(session, "session_id", ""),
        record.get("turn_key") or "",
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
    record["wakeup_state"] = wakeup_state
    records[delegation_id] = record
    session.async_delegation_origins = records
    _save(session)
    logger.info(
        "hermes_message_semantics action=background_task_status "
        "class=context_anchor kind=async_delegation_completion role=user "
        "content=%r session_id=%s turn_key=%s delegation_id=%s wakeup_state=%s",
        content,
        getattr(session, "session_id", ""),
        record.get("turn_key") or "",
        delegation_id,
        wakeup_state,
    )
    return dict(record)
