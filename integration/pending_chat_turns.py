"""Persistent helpers for chat turns submitted while a session is busy.

The route/streaming modules own locking and dispatch. This module only
normalizes queue records so the fork-specific queue shape stays out of the
large upstream files.
"""

from __future__ import annotations

import time
import uuid
from typing import Any


QUEUE_VERSION = 1


def pending_turns(session) -> list[dict[str, Any]]:
    """Return the session queue, repairing legacy/malformed values in memory."""
    raw = getattr(session, "pending_next_turns", None)
    if not isinstance(raw, list):
        raw = []
        session.pending_next_turns = raw
    normalized = [item for item in raw if isinstance(item, dict)]
    if len(normalized) != len(raw):
        session.pending_next_turns = normalized
        return normalized
    return raw


def find_pending_turn(session, idempotency_key: str | None):
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    for item in pending_turns(session):
        if str(item.get("idempotency_key") or "").strip() == key:
            return item
    return None


def enqueue_pending_turn(
    session,
    *,
    text: str,
    attachments: list | None,
    workspace: str,
    model: str,
    model_provider: str | None,
    idempotency_key: str | None,
    source: str = "queue",
    origin_stream_id: str | None = None,
    origin_generation: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append one durable turn, returning ``(record, created)``.

    Callers must hold the session control lock and persist the session after
    this function returns. A missing idempotency key is accepted for backwards
    compatibility, but callers should provide one to make retries exactly-once.
    """
    existing = find_pending_turn(session, idempotency_key)
    if existing is not None:
        return existing, False
    now = time.time()
    record: dict[str, Any] = {
        "queue_version": QUEUE_VERSION,
        "entry_id": uuid.uuid4().hex,
        "idempotency_key": str(idempotency_key or "").strip() or None,
        "session_id": str(getattr(session, "session_id", "") or ""),
        "text": str(text or ""),
        "attachments": list(attachments or []),
        "workspace": str(workspace or ""),
        "model": str(model or ""),
        "model_provider": str(model_provider or "").strip() or None,
        "created_at": now,
        "source": str(source or "queue"),
        "origin_stream_id": str(origin_stream_id or "").strip() or None,
        "origin_generation": origin_generation,
        "status": "queued",
        "attempts": 0,
        "dispatch_token": None,
        "last_error": None,
    }
    pending_turns(session).append(record)
    return record, True


def find_entry(session, entry_id: str | None):
    wanted = str(entry_id or "").strip()
    if not wanted:
        return None
    return next(
        (item for item in pending_turns(session) if str(item.get("entry_id") or "") == wanted),
        None,
    )
