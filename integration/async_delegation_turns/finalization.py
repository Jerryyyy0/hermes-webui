"""Terminal commit barrier for WebUI-owned async delegation wakeups."""

from __future__ import annotations

import logging
import copy
from collections.abc import Callable
from typing import Any

from integration.async_delegation_turns.events import committed_event
from integration.async_delegation_turns.state import (
    resolve_async_delegation_origin,
    settle_wakeup,
)

logger = logging.getLogger(__name__)


def commit_wakeup_after_outputs(
    session: Any,
    delegation_id: str,
    *,
    stream_id: str,
    generation: int | None,
    publish: Callable[[dict[str, Any]], None],
) -> dict[str, Any] | None:
    """Publish the durable transcript boundary, then settle the exact run.

    Callers must invoke this only after transcript/context/tool state, manifest
    artifacts and the completed turn-journal boundary are all durable.  The
    event is emitted before the final sidecar settlement so a crash between the
    two leaves a recoverable ``running`` record whose terminal journal proves
    that only this idempotent finalizer remains.
    """
    record = resolve_async_delegation_origin(session, delegation_id)
    if record is None:
        return None
    event = committed_event(
        session,
        delegation_id,
        record,
        stream_id=stream_id,
        message_count=len(getattr(session, "messages", None) or []),
    )
    try:
        publish(event)
    except Exception:
        # The sidecar and run journal remain authoritative for reconnect and
        # restart recovery; a transient live-view fan-out failure must not keep
        # an otherwise durable wakeup permanently running.
        logger.debug(
            "async turn committed live-view publish failed session_id=%s delegation_id=%s",
            getattr(session, "session_id", ""),
            delegation_id,
            exc_info=True,
        )
    records_before = copy.deepcopy(getattr(session, "async_delegation_origins", None))
    version_before = getattr(session, "async_delegation_activity_version", None)
    try:
        return settle_wakeup(
            session,
            delegation_id,
            stream_id=stream_id,
            generation=generation,
            # Preserve the Agent batch outcome. A failed child batch can still
            # have a successfully committed explanatory assistant wakeup.
            status=str(record.get("status") or "completed"),
            clear_prompt=True,
        )
    except Exception:
        # ``settle_wakeup`` applies the compact record before Session.save(). If
        # that save fails, restore the in-memory durable payload too so later
        # cleanup cannot accidentally persist a prompt-less running record.
        session.async_delegation_origins = records_before
        session.async_delegation_activity_version = version_before
        raise
