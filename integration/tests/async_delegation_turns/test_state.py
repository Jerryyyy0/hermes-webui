from types import SimpleNamespace

from integration.async_delegation_turns.state import (
    begin_async_delegation_cancellation,
    cancellation_status,
    mark_async_delegation_completion,
    mark_async_delegation_wakeup,
    record_async_delegation_dispatch,
    resolve_async_delegation_origin,
)
from integration.async_delegation_turns.events import (
    idle_event,
    is_idle,
    snapshot_event,
    task_event,
    unresolved_event,
)


def test_completion_status_log_excludes_content_and_turn_key_value(caplog):
    session = _session()
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    long_content = "y" * 650

    with caplog.at_level("DEBUG"):
        mark_async_delegation_completion(
            session,
            "deleg-1",
            wakeup_state="settled",
            content=long_content,
        )

    assert "action=background_task_status" in caplog.text
    assert "turn_key_present=True" in caplog.text
    assert ("y" * 650) not in caplog.text
    assert "turn:8" not in caplog.text


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


def test_dispatch_projects_lifecycle_state_to_its_origin_user_message():
    session = _session()
    session.messages = [
        {"role": "user", "content": "first", "_turn_key": "turn:7"},
        {"role": "user", "content": "dispatch", "_turn_key": "turn:8"},
    ]

    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )

    assert "async_delegations" not in session.messages[0]
    item = session.messages[1]["async_delegations"]["items"]["deleg-1"]
    assert session.messages[1]["async_delegations"]["state"] == "running"
    assert item["status"] == "running"
    assert item["cancel_state"] == "none"


def test_batch_dispatch_persists_child_metadata_and_snapshot_counts():
    session = _session()
    record = record_async_delegation_dispatch(
        session,
        {
            "status": "dispatched",
            "mode": "background",
            "delegation_id": "deleg-batch",
            "count": 2,
            "goals": ["research model A", "research model B"],
        },
        turn_key="turn:8",
    )

    assert record["child_task_count"] == 2
    assert record["goals"] == ["research model A", "research model B"]
    assert "delegation_kind" not in record

    snapshot = snapshot_event(session)
    assert snapshot["payload"]["active_delegation_count"] == 1
    assert snapshot["payload"]["active_child_task_count"] == 2
    assert snapshot["payload"]["tasks"][0]["child_task_count"] == 2
    assert "delegation_kind" not in snapshot["payload"]["tasks"][0]


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


def test_failed_batch_completion_is_exposed_in_the_lifecycle_envelope():
    session = _session()
    record_async_delegation_dispatch(
        session,
        {
            "status": "dispatched",
            "mode": "background",
            "delegation_id": "deleg-batch",
            "count": 2,
            "goals": ["research model A", "research model B"],
        },
        turn_key="turn:8",
    )

    completed = mark_async_delegation_completion(
        session,
        "deleg-batch",
        wakeup_state="queued",
        content="ignored",
        status="error",
        child_task_summary={"total": 2, "completed": 0, "failed": 1, "cancelled": 1},
    )
    event = task_event(session, "background_task_status", "deleg-batch", completed)

    assert event["payload"]["status"] == "failed"
    assert event["payload"]["child_task_summary"] == {
        "total": 2,
        "completed": 0,
        "failed": 1,
        "cancelled": 1,
    }


def test_lifecycle_events_share_a_persisted_versioned_envelope():
    session = _session()
    dispatched = record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    event = task_event(
        session,
        "background_task_dispatched",
        "deleg-1",
        dispatched,
        dispatched_at=dispatched["created_at"],
    )

    assert event["schema_version"] == 1
    assert event["event_type"] == "background_task_dispatched"
    assert event["event_id"] == "deleg-1:dispatched:1"
    assert event["background_activity_version"] == 1
    assert event["payload"] == {
        "delegation_id": "deleg-1",
        "child_task_count": 1,
        "goals": [],
        "origin_turn_key": "turn:8",
        "status": "running",
        "wakeup_state": "idle",
        "dispatched_at": dispatched["created_at"],
    }


def test_snapshot_excludes_settled_tasks_and_idle_uses_current_version():
    session = _session()
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    running_snapshot = snapshot_event(session)
    assert running_snapshot["payload"]["active_task_count"] == 1
    assert running_snapshot["payload"]["tasks"][0]["delegation_id"] == "deleg-1"

    mark_async_delegation_completion(
        session, "deleg-1", wakeup_state="settled", content="ignored"
    )
    assert is_idle(session) is True
    settled_snapshot = snapshot_event(session)
    assert settled_snapshot["payload"] == {
        "active_task_count": 0,
        "active_delegation_count": 0,
        "active_child_task_count": 0,
        "tasks": [],
    }
    idle = idle_event(session)
    assert idle["event_type"] == "background_tasks_idle"
    assert idle["background_activity_version"] == 2
    assert idle["payload"]["active_task_count"] == 0


def test_cancellation_scope_is_stable_and_projects_to_origin_messages():
    session = _session()
    session.messages = [
        {"role": "user", "content": "one", "_turn_key": "turn:8"},
        {"role": "user", "content": "two", "_turn_key": "turn:9"},
    ]
    for delegation_id, turn_key in (("deleg-1", "turn:8"), ("deleg-2", "turn:9")):
        record_async_delegation_dispatch(
            session,
            {"status": "dispatched", "mode": "background", "delegation_id": delegation_id},
            turn_key=turn_key,
        )

    cancellation = begin_async_delegation_cancellation(session, now=10.0)
    assert cancellation == {
        "state": "cancelling",
        "delegation_ids": ["deleg-1", "deleg-2"],
        "requested_at": 10.0,
        "settled_at": None,
    }
    assert begin_async_delegation_cancellation(session, now=11.0) == cancellation
    assert session.messages[0]["async_delegations"]["state"] == "cancelling"
    assert session.messages[1]["async_delegations"]["items"]["deleg-2"]["cancel_state"] == "requested"

    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-new"},
        turn_key="turn:9",
    )
    assert cancellation_status(session)["delegation_ids"] == ["deleg-1", "deleg-2"]


def test_unresolved_event_is_a_versioned_failure_envelope():
    session = _session()
    session.async_delegation_activity_version = 4

    event = unresolved_event(session, "deleg-missing")

    assert event["schema_version"] == 1
    assert event["event_id"] == "deleg-missing:unresolved:4"
    assert event["event_type"] == "background_task_unresolved"
    assert event["session_id"] == "session-1"
    assert event["background_activity_version"] == 4
    assert event["payload"] == {
        "delegation_id": "deleg-missing",
        "reason": "origin_unresolved",
        "retryable": False,
    }
