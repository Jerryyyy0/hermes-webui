from types import SimpleNamespace

from integration.async_delegation_turns.state import record_async_delegation_dispatch


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _session():
    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={},
        messages=[{"role": "user", "content": "task", "_turn_key": "turn:8"}],
        saves=0,
    )

    def save(*, touch_updated_at=False):
        session.saves += 1

    session.save = save
    return session


def test_cancel_post_snapshots_then_returns_accepted(monkeypatch):
    from integration.async_delegation_turns import handlers

    session = _session()
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    captured = {}
    interrupts = []

    monkeypatch.setattr(handlers, "_session_or_error", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(handlers, "_get_session_agent_lock", lambda *_: _NoopLock())
    monkeypatch.setattr(
        handlers,
        "j",
        lambda _handler, payload, status=200: captured.update(payload=payload, status=status),
    )
    monkeypatch.setattr(
        handlers,
        "_interrupt_in_background",
        lambda session_id, delegation_ids: interrupts.append((session_id, delegation_ids)),
    )

    class _Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(handlers.threading, "Thread", _Thread)

    assert handlers.try_handle_post(
        SimpleNamespace(headers={}),
        SimpleNamespace(path="/api/sessions/background_tasks/cancel"),
        {"session_id": "session-1"},
    ) is True

    assert captured == {
        "status": 202,
        "payload": {
            "session_id": "session-1",
            "state": "cancelling",
            "delegation_ids": ["deleg-1"],
            "requested_at": captured["payload"]["requested_at"],
            "settled_at": None,
        },
    }
    assert interrupts == [("session-1", ["deleg-1"])]


def test_cancel_get_reads_status_without_starting_an_interrupt(monkeypatch):
    from integration.async_delegation_turns import handlers

    session = _session()
    captured = {}
    monkeypatch.setattr(handlers, "_session_or_error", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(
        handlers,
        "j",
        lambda _handler, payload, status=200: captured.update(payload=payload, status=status),
    )

    assert handlers.try_handle_get(
        SimpleNamespace(headers={}),
        SimpleNamespace(path="/api/sessions/background_tasks/cancel", query="session_id=session-1"),
    ) is True
    assert captured == {
        "status": 200,
        "payload": {
            "session_id": "session-1",
            "state": "idle",
            "delegation_ids": [],
            "requested_at": None,
            "settled_at": None,
        },
    }


def test_cancel_accepts_explicit_profile_without_cookie(monkeypatch):
    from integration.async_delegation_turns import handlers

    session = _session()
    captured = {}
    seen = {}
    monkeypatch.setattr(
        handlers,
        "_session_or_error",
        lambda _handler, session_id, *, profile_override=None: (
            seen.update(session_id=session_id, profile=profile_override) or session
        ),
    )
    monkeypatch.setattr(
        handlers,
        "j",
        lambda _handler, payload, status=200: captured.update(payload=payload, status=status),
    )

    assert handlers.try_handle_get(
        SimpleNamespace(headers={}),
        SimpleNamespace(
            path="/api/sessions/background_tasks/cancel",
            query="session_id=session-1&profile=abc",
        ),
    ) is True

    assert seen == {"session_id": "session-1", "profile": "abc"}
    assert captured["status"] == 200
