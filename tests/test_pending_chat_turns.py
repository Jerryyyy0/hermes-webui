"""Regression tests for durable chat/start queueing while a turn is busy."""

from __future__ import annotations

import queue

from api import config
from api import routes
from integration.pending_chat_turns import enqueue_pending_turn, find_pending_turn


class _Session:
    session_id = "queued-session"
    active_stream_id = None
    active_stream_generation = 3
    control_generation = 3
    pending_user_message = None
    pending_attachments = []
    pending_started_at = None
    messages = []
    title = "Queued"
    workspace = "/tmp"
    model = "test-model"
    model_provider = None

    def __init__(self):
        self.pending_next_turns = []
        self.saved = 0

    def save(self, *args, **kwargs):
        self.saved += 1


def test_pending_turn_is_idempotent():
    session = _Session()
    first, created = enqueue_pending_turn(
        session,
        text="hello",
        attachments=[],
        workspace="/tmp",
        model="test-model",
        model_provider=None,
        idempotency_key="request-1",
    )
    second, created_again = enqueue_pending_turn(
        session,
        text="hello",
        attachments=[],
        workspace="/tmp",
        model="test-model",
        model_provider=None,
        idempotency_key="request-1",
    )

    assert created is True
    assert created_again is False
    assert second["entry_id"] == first["entry_id"]
    assert len(session.pending_next_turns) == 1


def test_busy_chat_start_persists_queue_and_deduplicates(monkeypatch, tmp_path):
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = _Session()
    old_stream_id = "old-stream"
    config.register_active_run(old_stream_id, session_id=session.session_id, phase="cancelling")
    monkeypatch.setattr(routes, "_get_session_agent_lock", config._get_session_agent_lock)

    try:
        first = routes._start_chat_stream_for_session(
            session,
            msg="next message",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
            idempotency_key="request-2",
        )
        second = routes._start_chat_stream_for_session(
            session,
            msg="next message",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
            idempotency_key="request-2",
        )

        assert first["_status"] == 202
        assert second["_status"] == 202
        assert first["entry_id"] == second["entry_id"]
        assert len(session.pending_next_turns) == 1
        assert session.pending_next_turns[0]["status"] == "queued"
        assert session.pending_next_turns[0]["text"] == "next message"
    finally:
        config.unregister_active_run(old_stream_id)


def test_drain_marks_entry_sent_only_after_successor_stream(monkeypatch, tmp_path):
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = _Session()
    entry, _ = enqueue_pending_turn(
        session,
        text="drain me",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        idempotency_key="request-3",
    )
    sessions = {session.session_id: session}
    monkeypatch.setattr(routes, "get_session", lambda sid: sessions[sid])
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda *args, **kwargs: {"stream_id": "successor-stream", "_status": 200},
    )

    result = routes.drain_pending_chat_turn(session.session_id)

    assert result["stream_id"] == "successor-stream"
    assert entry["status"] == "sent"
    assert entry["stream_id"] == "successor-stream"
    assert entry["dispatch_token"] is None
    assert session.saved >= 2
