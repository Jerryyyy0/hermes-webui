"""Crash/stale-stream recovery for async delegation wakeups."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def reconcile_async_wakeup_before_stale_cleanup(
    session: Any,
    *,
    stream_id: str | None = None,
    require_pending_source: bool = True,
) -> bool:
    """Convert an orphaned async wakeup into a durable failed item.

    Generic stale repair must not erase the wakeup prompt before its delegation
    provenance is settled. This helper is intentionally side-effect free apart
    from mutating the supplied session object; the caller owns the lock/save.
    It returns ``True`` when it handled an async wakeup and the generic repair
    should stop.
    """
    if session is None:
        return False
    if require_pending_source and getattr(session, "pending_user_source", None) != "async_delegation_wakeup":
        return False
    active = str(getattr(session, "active_stream_id", None) or "").strip()
    expected = str(stream_id or active).strip()
    if not expected:
        return False
    if active and expected and active != expected:
        return False
    records = getattr(session, "async_delegation_origins", None)
    if not isinstance(records, dict):
        return False
    matched = None
    for delegation_id, raw in records.items():
        if not isinstance(raw, dict):
            continue
        wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
        if str(wakeup.get("stream_id") or "").strip() == expected:
            matched = (str(delegation_id), dict(raw), dict(wakeup))
            break
    if matched is None:
        # Legacy sidecars may have persisted a single running delegation
        # before stream_id was added.  Migrate only when provenance is
        # unambiguous; zero/multiple candidates fail closed.
        legacy = []
        for delegation_id, raw in records.items():
            if not isinstance(raw, dict) or raw.get("wakeup_state") != "running":
                continue
            wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
            if not str(wakeup.get("stream_id") or "").strip():
                legacy.append((str(delegation_id), dict(raw), dict(wakeup)))
        if len(legacy) == 1:
            delegation_id, record, wakeup = legacy[0]
            wakeup["stream_id"] = expected
            matched = (delegation_id, record, wakeup)
    if matched is None:
        return False
    delegation_id, record, wakeup = matched
    if str(record.get("wakeup_state") or "") not in {"running", "queued"}:
        return False
    terminal_state = ""
    try:
        from api.run_journal import latest_run_summary

        summary = latest_run_summary(str(getattr(session, "session_id", "")), expected)
        if summary.get("terminal"):
            terminal_state = str(summary.get("terminal_state") or "").strip()
    except Exception:
        terminal_state = ""
    if terminal_state == "completed":
        record["wakeup_state"] = "settled"
        # The terminal run proves that the explanatory wakeup committed; it
        # does not rewrite the child batch outcome (which may be ``failed``).
        if str(record.get("status") or "running") == "running":
            record["status"] = "completed"
        record.pop("wakeup", None)
    elif terminal_state == "interrupted-by-user":
        record["wakeup_state"] = "settled"
        record["status"] = "cancelled"
        record["cancel_state"] = "cancelled"
        record.pop("wakeup", None)
    else:
        # Keep the complete prompt for operator retry/inspection, but make the
        # orphaned execution terminal so no worker can be started from stale state.
        wakeup["stream_id"] = None
        wakeup["error_code"] = (
            "server_restarted_during_wakeup"
            if not terminal_state
            else f"wakeup_{terminal_state}"
        )
        record["wakeup"] = wakeup
        record["wakeup_state"] = "failed"
        # The missing terminal journal proves only that the WebUI wakeup
        # stream did not settle.  ``status`` is the Agent delegation outcome
        # and must remain authoritative (for example, ``completed`` may have
        # been persisted before this stream was started).
    from integration.async_delegation_turns.state import _apply_records, _bump_activity_version

    record["activity_version"] = _bump_activity_version(session)
    records[delegation_id] = record
    # Re-project the sidecar onto the origin user message before the caller's
    # save.  Recovery callers intentionally own persistence, so use the
    # no-save helper rather than _persist().
    _apply_records(session, records)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.pending_turn_key = None
    logger.info(
        "async_delegation_wakeup_%s session_id=%s delegation_id=%s stream_id=%s terminal_state=%s",
        "settled" if record.get("wakeup_state") == "settled" else "interrupted",
        getattr(session, "session_id", ""),
        delegation_id,
        expected,
        terminal_state or "missing",
    )
    return True
