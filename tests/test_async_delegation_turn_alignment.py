from types import SimpleNamespace
import queue


def test_async_wakeup_uses_origin_turn_and_hidden_anchor(monkeypatch):
    from api import background_process as bp

    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:8",
                "status": "running",
                "wakeup_state": "idle",
            }
        },
        save=lambda **_: None,
    )
    registry = SimpleNamespace(completion_queue=None)
    claim = SimpleNamespace(durable=False)
    started = {}

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "complete_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda *_: False)
    monkeypatch.setattr(bp, "_emit_async_delegation_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "api.models.get_session",
        lambda _sid: session,
    )

    def start(*args, **kwargs):
        started.update(kwargs)

    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", start)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())

    bp._process_async_delegation_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg-1",
            "results": [{"status": "completed", "summary": "done"}],
        },
        session_id="session-1",
        delegation_id="deleg-1",
        process_registry=registry,
    )

    assert started["origin_turn_key"] == "turn:8"


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_async_wakeup_is_queued_while_foreground_turn_is_active(monkeypatch):
    """A completion never starts a second Agent stream for the same session."""
    from api import background_process as bp

    queued = queue.Queue()
    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={
            "deleg-1": {"turn_key": "turn:8", "status": "running", "wakeup_state": "idle"}
        },
        save=lambda **_: None,
    )
    registry = SimpleNamespace(completion_queue=queued)
    claim = SimpleNamespace(durable=False)
    started = []
    statuses = []

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda *_: True)
    monkeypatch.setattr(bp, "_emit_async_delegation_status", lambda *args, **kwargs: statuses.append(kwargs))
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", lambda *args, **kwargs: started.append(kwargs))
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())

    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-1",
        "results": [{"status": "completed", "summary": "done"}],
    }
    bp._process_async_delegation_event(
        event,
        session_id="session-1",
        delegation_id="deleg-1",
        process_registry=registry,
    )

    assert started == []
    assert queued.get_nowait() == event
    assert session.async_delegation_origins["deleg-1"]["wakeup_state"] == "queued"
    assert statuses[-1]["wakeup_state"] == "queued"


def test_async_wakeup_terminal_state_is_published_to_session_sse(monkeypatch):
    """The browser learns that the server-side wakeup has fully settled."""
    from api import background_process as bp

    emitted = []
    monkeypatch.setattr(
        bp,
        "_emit_to_session_streams",
        lambda session_id, event, payload: emitted.append((session_id, event, payload)),
    )

    bp.emit_async_delegation_status(
        "session-1",
        "deleg-1",
        {"turn_key": "turn:8"},
        status="completed",
        wakeup_state="settled",
        content="final assistant",
    )

    assert emitted == [
        (
            "session-1",
            "background_task_status",
            {
                "session_id": "session-1",
                "delegation_id": "deleg-1",
                "origin_turn_key": "turn:8",
                "status": "completed",
                "wakeup_state": "settled",
            },
        )
    ]
