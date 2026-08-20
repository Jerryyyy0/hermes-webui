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


def test_cancelled_completion_settles_without_starting_a_wakeup(monkeypatch):
    from api import background_process as bp
    from integration.async_delegation_turns import begin_async_delegation_cancellation

    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={
            "deleg-1": {"delegation_id": "deleg-1", "turn_key": "turn:8", "status": "running", "wakeup_state": "idle"}
        },
        async_delegation_activity_version=0,
        messages=[{"role": "user", "_turn_key": "turn:8"}],
        save=lambda **_: None,
    )
    begin_async_delegation_cancellation(session)
    registry = SimpleNamespace(completion_queue=None)
    claim = SimpleNamespace(durable=False)
    started = []
    emitted = []

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "complete_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(bp, "_emit_async_delegation_status", lambda *args, **kwargs: emitted.append(kwargs))
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())

    bp._process_async_delegation_event(
        {"type": "async_delegation", "delegation_id": "deleg-1", "status": "cancelled"},
        session_id="session-1",
        delegation_id="deleg-1",
        process_registry=registry,
    )

    assert started == []
    assert emitted[-1]["wakeup_state"] == "settled"
    assert session.async_delegation_origins["deleg-1"]["cancel_state"] == "cancelled"
    assert session.async_delegation_cancellation["state"] == "settled"


def test_cancelled_children_override_inconsistent_batch_error_during_cancellation(monkeypatch):
    """A fully interrupted batch must not appear as a failed cancellation."""
    from api import background_process as bp
    from integration.async_delegation_turns import begin_async_delegation_cancellation

    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:8",
                "status": "running",
                "wakeup_state": "idle",
                "child_task_count": 2,
            }
        },
        async_delegation_activity_version=0,
        messages=[{"role": "user", "_turn_key": "turn:8"}],
        save=lambda **_: None,
    )
    begin_async_delegation_cancellation(session)
    registry = SimpleNamespace(completion_queue=None)
    claim = SimpleNamespace(durable=False)
    started = []

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "complete_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(
        bp, "_start_async_delegation_wakeup_turn", lambda *args, **kwargs: started.append((args, kwargs))
    )
    monkeypatch.setattr(bp, "_emit_async_delegation_status", lambda *args, **kwargs: None)
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())

    bp._process_async_delegation_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg-1",
            "status": "error",
            "results": [{"status": "cancelled"}, {"status": "cancelled"}],
        },
        session_id="session-1",
        delegation_id="deleg-1",
        process_registry=registry,
    )

    record = session.async_delegation_origins["deleg-1"]
    assert started == []
    assert record["status"] == "cancelled"
    assert record["cancel_state"] == "cancelled"
    assert session.messages[0]["async_delegations"]["state"] == "cancelled"


def test_unresolved_completion_emits_terminal_event_after_retry_is_exhausted(monkeypatch):
    from api import background_process as bp

    emitted = []
    registry = SimpleNamespace(completion_queue=None)
    claim = SimpleNamespace(durable=False)
    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={},
        async_delegation_activity_version=2,
        save=lambda **_: None,
    )

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "complete_async_delegation_delivery", lambda *_: None)
    monkeypatch.setattr(bp, "_retry_unmapped_async_delegation_event", lambda *_: False)
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())
    monkeypatch.setattr(
        bp,
        "emit_session_channel_event",
        lambda session_id, event, payload: emitted.append((session_id, event, payload)),
    )

    bp._process_async_delegation_event(
        {"type": "async_delegation", "delegation_id": "deleg-missing"},
        session_id="session-1",
        delegation_id="deleg-missing",
        process_registry=registry,
    )

    assert emitted[0][0:2] == ("session-1", "background_task_unresolved")
    assert emitted[0][2]["payload"] == {
        "delegation_id": "deleg-missing",
        "reason": "origin_unresolved",
        "retryable": False,
    }


def test_async_batch_error_is_published_as_failed(monkeypatch):
    """An all-failed Agent batch still wakes the parent but keeps its failure state."""
    from api import background_process as bp

    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={
            "deleg-batch": {
                "turn_key": "turn:8",
                "status": "running",
                "wakeup_state": "idle",
                "child_task_count": 2,
            }
        },
        save=lambda **_: None,
    )
    registry = SimpleNamespace(completion_queue=None)
    claim = SimpleNamespace(durable=False)
    statuses = []
    wakeups = []

    monkeypatch.setattr(bp, "claim_async_delegation_delivery", lambda *_: claim)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda *_: False)
    monkeypatch.setattr(
        bp, "_start_async_delegation_wakeup_turn", lambda *_, **kwargs: wakeups.append(kwargs)
    )
    monkeypatch.setattr(
        bp, "_emit_async_delegation_status", lambda *_, **kwargs: statuses.append(kwargs)
    )
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())

    bp._process_async_delegation_event(
        {
            "type": "async_delegation",
            "delegation_id": "deleg-batch",
            "status": "error",
            "is_batch": True,
            "goals": ["research model A", "research model B"],
            "results": [
                {"status": "error", "summary": "provider unavailable"},
                {"status": "cancelled", "summary": "cancelled"},
            ],
        },
        session_id="session-1",
        delegation_id="deleg-batch",
        process_registry=registry,
    )

    assert session.async_delegation_origins["deleg-batch"]["status"] == "failed"
    assert session.async_delegation_origins["deleg-batch"]["child_task_summary"] == {
        "total": 2,
        "completed": 0,
        "failed": 1,
        "cancelled": 1,
    }
    assert statuses[-1]["status"] == "failed"
    assert len(wakeups) == 1


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


def test_async_wakeup_status_uses_versioned_lifecycle_envelope(monkeypatch):
    from api import background_process as bp

    emitted = []
    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_activity_version=3,
        async_delegation_origins={
            "deleg-1": {
                "delegation_id": "deleg-1",
                "turn_key": "turn:8",
                "status": "completed",
                "wakeup_state": "settled",
                "activity_version": 3,
                "delegation_kind": "single",
            }
        },
    )
    monkeypatch.setattr("api.models.get_session", lambda _sid: session)
    monkeypatch.setattr(
        bp,
        "_emit_to_session_streams",
        lambda session_id, event, payload: emitted.append((session_id, event, payload)),
    )

    bp.emit_async_delegation_status(
        "session-1",
        "deleg-1",
        session.async_delegation_origins["deleg-1"],
        status="completed",
        wakeup_state="settled",
        content="final assistant",
    )

    status = emitted[0]
    assert status[0:2] == ("session-1", "background_task_status")
    assert status[2]["event_id"] == "deleg-1:status:3"
    assert status[2]["event_type"] == "background_task_status"
    assert status[2]["background_activity_version"] == 3
    assert status[2]["payload"] == {
        "delegation_id": "deleg-1",
        "child_task_count": 1,
        "origin_turn_key": "turn:8",
        "status": "completed",
        "wakeup_state": "settled",
    }
    assert emitted[1][1] == "background_tasks_idle"
    assert emitted[1][2]["event_type"] == "background_tasks_idle"


def test_session_response_hides_internal_async_delegation_sidecars():
    from api.models import Session

    session = Session(
        session_id="delegation-public-view",
        workspace="/tmp",
        messages=[
            {
                "role": "user",
                "_turn_key": "turn:8",
                "async_delegations": {"state": "running", "items": {}},
            }
        ],
        async_delegation_origins={"deleg-1": {"turn_key": "turn:8"}},
        async_delegation_activity_version=3,
        async_delegation_cancellation={"state": "cancelling", "delegation_ids": ["deleg-1"]},
    )

    response = session.compact() | {"messages": session.messages}

    assert "async_delegation_origins" not in response
    assert "async_delegation_activity_version" not in response
    assert "async_delegation_cancellation" not in response
    assert response["messages"][0]["async_delegations"]["state"] == "running"


def test_async_wakeup_display_assistant_keeps_origin_identity():
    """The visible wakeup answer is identifiable without exposing its anchor."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous = [
        {"role": "user", "content": "派发后台任务", "_turn_key": "turn:8"},
        {"role": "assistant", "content": "已派发", "_turn_key": "turn:8"},
    ]
    result = previous + [
        {
            "role": "user",
            "content": "[ASYNC DELEGATION BATCH COMPLETE - deleg-1]",
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "async_delegation_completion",
        },
        {"role": "assistant", "content": "后台任务最终回复"},
    ]

    visible = _merge_display_messages_after_agent_result(
        previous,
        previous,
        result,
        "[ASYNC DELEGATION BATCH COMPLETE - deleg-1]",
        source="async_delegation_wakeup",
        canonical_turn_key="turn:8",
        async_delegation_id="deleg-1",
    )

    assert visible[-1] == {
        "role": "assistant",
        "content": "后台任务最终回复",
        "_turn_key": "turn:8",
        "_source": "async_delegation_wakeup",
        "delegation_id": "deleg-1",
    }
    assert not any(
        message.get("_hermes_scaffold_kind") == "async_delegation_completion"
        for message in visible
        if isinstance(message, dict)
    )
