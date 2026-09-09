"""Live-view publishing at async wakeup lifecycle boundaries."""

from __future__ import annotations

import logging
from typing import Any

from integration.async_delegation_turns.events import task_event
from integration.async_delegation_turns.state import resolve_async_delegation_origin

logger = logging.getLogger(__name__)


def publish_server_turn_started(
    session: Any,
    delegation_id: str,
    *,
    stream_id: str,
    source: str,
) -> int:
    """Publish the attachable stream identity before its worker can run."""
    try:
        from api.background_process import get_session_channel

        channel = get_session_channel(str(getattr(session, "session_id", "") or ""))
        if channel is None:
            return 0
        record = resolve_async_delegation_origin(session, delegation_id)
        if record is None:
            return 0
        payload = task_event(
            session,
            "server_turn_started",
            delegation_id,
            record,
            stream_id=str(stream_id),
            source=str(source or ""),
        )
        return channel.emit("server_turn_started", payload)
    except Exception:
        logger.debug(
            "server_turn_started pre-worker publish failed session_id=%s delegation_id=%s",
            getattr(session, "session_id", ""),
            delegation_id,
            exc_info=True,
        )
        return 0
