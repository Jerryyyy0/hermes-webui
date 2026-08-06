from types import SimpleNamespace

from integration.async_delegation_turns.state import (
    mark_async_delegation_completion,
    mark_async_delegation_wakeup,
    record_async_delegation_dispatch,
    resolve_async_delegation_origin,
)


def _session():
    session = SimpleNamespace(
        session_id="session-1",
        async_delegation_origins={},
        saves=0,
    )

    def save(*, touch_updated_at=False):
        session.saves += 1

    session.save = save
    return session


def test_dispatch_records_immutable_origin_turn():
    session = _session()
    record = record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )

    assert record["turn_key"] == "turn:8"
    assert resolve_async_delegation_origin(session, "deleg-1")["turn_key"] == "turn:8"
    assert session.async_delegation_origins["deleg-1"]["wakeup_state"] == "idle"


def test_completion_status_is_non_transcript_and_settles():
    session = _session()
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    completed = mark_async_delegation_completion(
        session,
        "deleg-1",
        wakeup_state="running",
        content="[ASYNC DELEGATION BATCH COMPLETE -- result]",
    )
    assert completed["status"] == "completed"
    assert completed["wakeup_state"] == "running"
    settled = mark_async_delegation_wakeup(
        session,
        "deleg-1",
        wakeup_state="settled",
        content="final assistant",
    )
    assert settled["turn_key"] == "turn:8"
    assert settled["wakeup_state"] == "settled"
