from types import SimpleNamespace

from integration.async_delegation_turns.state import (
    begin_async_delegation_cancellation,
    cancellation_status,
    mark_async_delegation_completion,
    mark_async_delegation_wakeup,
    record_async_delegation_dispatch,
    record_retryable_wakeup_start_failure,
    resolve_async_delegation_origin,
    receive_completion,
    select_next_queued_wakeup,
    prepare_wakeup_start_locked,
    requeue_unstarted_wakeup,
    running_wakeup_stream_ids,
    settle_wakeup,
    validate_wakeup_start_locked,
)
from integration.async_delegation_turns.recovery import reconcile_async_wakeup_before_stale_cleanup
from integration.async_delegation_turns.finalization import commit_wakeup_after_outputs
from integration.async_delegation_turns.events import (
    committed_event,
    idle_event,
    is_idle,
    snapshot_event,
    task_event,
    unresolved_event,
)


def test_committed_event_identifies_the_persisted_wakeup_stream():
    session = _session()
    session.async_delegation_activity_version = 4
    record = {
        "turn_key": "turn:8",
        "status": "completed",
        "wakeup_state": "running",
        "activity_version": 4,
    }

    event = committed_event(
        session,
        "deleg-1",
        record,
        stream_id="stream-1",
        message_count=6,
    )

    assert event["event_type"] == "async_turn_committed"
    assert event["event_id"] == "deleg-1:turn-committed:4"
    assert event["payload"]["stream_id"] == "stream-1"
    assert event["payload"]["message_count"] == 6
    assert event["payload"]["origin_turn_key"] == "turn:8"


def test_committed_event_precedes_prompt_cleanup_and_preserves_batch_status():
    session = _session()
    session.messages = [{"role": "assistant", "content": "final"}]
    session.active_stream_generation = None
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "failed",
            "wakeup_state": "running",
            "activity_version": 4,
            "wakeup": {
                "prompt": "durable completion",
                "stream_id": "stream-1",
            },
        }
    }
    observed = []

    settled = commit_wakeup_after_outputs(
        session,
        "deleg-1",
        stream_id="stream-1",
        generation=3,
        publish=lambda event: observed.append(
            (
                event["event_type"],
                session.async_delegation_origins["deleg-1"]["wakeup"]["prompt"],
            )
        ),
    )

    assert observed == [("async_turn_committed", "durable completion")]
    assert settled["status"] == "failed"
    assert settled["wakeup_state"] == "settled"
    assert "wakeup" not in session.async_delegation_origins["deleg-1"]


def test_failed_final_settlement_restores_in_memory_durable_prompt():
    session = _session()
    session.messages = [{"role": "assistant", "content": "final"}]
    session.active_stream_generation = None
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "activity_version": 4,
            "wakeup": {
                "prompt": "durable completion",
                "stream_id": "stream-1",
            },
        }
    }
    session.save = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full"))

    import pytest

    with pytest.raises(OSError, match="disk full"):
        commit_wakeup_after_outputs(
            session,
            "deleg-1",
            stream_id="stream-1",
            generation=3,
            publish=lambda _event: None,
        )

    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "running"
    assert record["wakeup"]["prompt"] == "durable completion"


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


def test_receive_completion_persists_hidden_wakeup_inbox_before_ack():
    session = _session()
    session.messages = [{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}]
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )

    received = receive_completion(
        session,
        "deleg-1",
        prompt="[ASYNC DELEGATION BATCH COMPLETE -- result]",
    )

    assert received["wakeup_state"] == "queued"
    assert received["wakeup"]["prompt"].startswith("[ASYNC")
    assert received["wakeup"]["error_code"] == "agent_ack_pending"
    assert select_next_queued_wakeup(session) is None


def test_receive_completion_does_not_resurrect_settled_duplicate():
    session = _session()
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "settled",
            "cancel_state": "none",
        }
    }

    result = receive_completion(session, "deleg-1", prompt="replayed completion")

    assert result["wakeup_state"] == "settled"
    assert "wakeup" not in session.async_delegation_origins["deleg-1"]
    assert select_next_queued_wakeup(session) is None


def test_wakeup_admission_is_one_sidecar_mutation_and_settlement_is_fenced():
    session = _session()
    session.messages = [{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}]
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    receive_completion(session, "deleg-1", prompt="completion", ack_pending=False)
    selected = select_next_queued_wakeup(session)
    assert selected[0] == "deleg-1"

    session.active_stream_generation = 3
    saves_before_admission = session.saves
    admitted = prepare_wakeup_start_locked(
        session,
        "deleg-1",
        stream_id="stream-1",
        turn_key="turn:8",
        generation=3,
    )
    assert admitted["wakeup_state"] == "running"
    assert admitted["wakeup"]["stream_id"] == "stream-1"
    assert admitted["wakeup"]["start_attempts"] == 1
    assert session.saves == saves_before_admission
    assert settle_wakeup(
        session,
        "deleg-1",
        stream_id="stream-other",
        generation=3,
    ) is None
    settled = settle_wakeup(
        session,
        "deleg-1",
        stream_id="stream-1",
        generation=3,
    )
    assert settled["wakeup_state"] == "settled"
    assert "wakeup" not in session.async_delegation_origins["deleg-1"]


def test_stale_recovery_preserves_prompt_and_marks_orphan_failed():
    session = _session()
    session.messages = [{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}]
    session.pending_user_source = "async_delegation_wakeup"
    session.active_stream_id = "stream-1"
    session.pending_user_message = "hidden wakeup"
    session.pending_attachments = []
    session.pending_started_at = 1
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "wakeup": {"prompt": "durable completion", "stream_id": "stream-1"},
        }
    }

    assert reconcile_async_wakeup_before_stale_cleanup(session, stream_id="stream-1")
    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "failed"
    # Restarting WebUI interrupts only the parent wakeup stream; the Agent
    # completion outcome must remain authoritative and must not be rewritten.
    assert record["status"] == "completed"
    assert record["wakeup"]["prompt"] == "durable completion"
    assert record["wakeup"]["error_code"] == "server_restarted_during_wakeup"
    projected = session.messages[0]["async_delegations"]
    assert projected["state"] == "settled"
    assert projected["items"]["deleg-1"]["status"] == "completed"
    assert projected["items"]["deleg-1"]["wakeup_state"] == "failed"
    assert session.active_stream_id is None
    assert session.pending_user_message is None


def test_stale_recovery_migrates_unambiguous_legacy_running_record():
    session = _session()
    session.pending_user_source = "async_delegation_wakeup"
    session.active_stream_id = "stream-legacy"
    session.pending_user_message = "hidden wakeup"
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "wakeup": {"prompt": "durable completion"},
        }
    }

    assert reconcile_async_wakeup_before_stale_cleanup(session, stream_id="stream-legacy")
    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup"]["stream_id"] is None
    assert record["wakeup_state"] == "failed"


def test_stale_recovery_settles_after_terminal_journal(monkeypatch):
    session = _session()
    session.pending_user_source = "async_delegation_wakeup"
    session.active_stream_id = "stream-1"
    session.pending_user_message = "hidden wakeup"
    session.pending_attachments = []
    session.pending_started_at = 1
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "wakeup": {"prompt": "durable completion", "stream_id": "stream-1"},
        }
    }
    monkeypatch.setattr(
        "api.run_journal.latest_run_summary",
        lambda *_args, **_kwargs: {"terminal": True, "terminal_state": "completed"},
    )

    assert reconcile_async_wakeup_before_stale_cleanup(session, stream_id="stream-1")
    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "settled"
    assert record["status"] == "completed"
    assert "wakeup" not in record


def test_stale_recovery_preserves_failed_batch_outcome_after_committed_wakeup(monkeypatch):
    session = _session()
    session.pending_user_source = "async_delegation_wakeup"
    session.active_stream_id = "stream-1"
    session.pending_user_message = "hidden wakeup"
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "failed",
            "wakeup_state": "running",
            "wakeup": {"prompt": "durable completion", "stream_id": "stream-1"},
        }
    }
    monkeypatch.setattr(
        "api.run_journal.latest_run_summary",
        lambda *_args, **_kwargs: {"terminal": True, "terminal_state": "completed"},
    )

    assert reconcile_async_wakeup_before_stale_cleanup(session, stream_id="stream-1")
    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "settled"
    assert record["status"] == "failed"
    assert record["activity_version"] == 1


def test_generic_stale_repair_fails_closed_when_async_provenance_is_missing(tmp_path):
    from api.models import _apply_core_sync_or_error_marker

    session = _session()
    session.messages = []
    session.context_messages = []
    session.pending_user_source = "async_delegation_wakeup"
    session.active_stream_id = "stream-orphan"
    session.pending_user_message = "completion"
    session.pending_attachments = []
    session.pending_started_at = 1
    session.pending_turn_key = "turn:8"

    assert not _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing.json",
        stream_id_for_recheck="stream-orphan",
        require_stream_dead=False,
    )
    assert session.active_stream_id == "stream-orphan"
    assert session.pending_user_message == "completion"
    assert session.saves == 0


def test_cancelled_wakeup_fails_validation_without_mutating_session():
    session = _session()
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "queued",
            "cancel_state": "requested",
            "wakeup": {"prompt": "completion", "stream_id": None},
        }
    }
    before = dict(session.async_delegation_origins["deleg-1"])

    assert validate_wakeup_start_locked(session, "deleg-1", turn_key="turn:8") is None
    assert session.async_delegation_origins["deleg-1"] == before
    assert session.saves == 0


def test_fifth_worker_dispatch_failure_is_terminal():
    session = _session()
    session.active_stream_generation = 9
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "cancel_state": "none",
            "wakeup": {
                "prompt": "completion",
                "stream_id": "stream-5",
                "start_attempts": 5,
            },
        }
    }

    failed = requeue_unstarted_wakeup(
        session,
        "deleg-1",
        stream_id="stream-5",
        generation=9,
        error_code="worker_dispatch_failed",
    )

    assert failed["wakeup_state"] == "failed"
    assert failed["status"] == "failed"
    assert failed["wakeup"]["error_code"] == "start_retry_exhausted"
    assert select_next_queued_wakeup(session) is None


def test_retryable_pre_dispatch_failure_counts_once_and_exhausts():
    session = _session()
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "queued",
            "cancel_state": "none",
            "wakeup": {"prompt": "completion", "start_attempts": 4},
        }
    }

    failed = record_retryable_wakeup_start_failure(
        session,
        "deleg-1",
        previous_attempts=4,
        error_code="start_retryable_failure",
    )

    assert failed["wakeup_state"] == "failed"
    assert failed["wakeup"]["start_attempts"] == 5
    assert failed["wakeup"]["error_code"] == "start_retry_exhausted"


def test_cancellation_barrier_exposes_exact_running_wakeup_stream():
    session = _session()
    session.async_delegation_origins = {
        "deleg-1": {
            "turn_key": "turn:8",
            "status": "completed",
            "wakeup_state": "running",
            "cancel_state": "none",
            "wakeup": {"prompt": "completion", "stream_id": "stream-1"},
        },
        "deleg-2": {
            "turn_key": "turn:9",
            "status": "completed",
            "wakeup_state": "queued",
            "cancel_state": "none",
            "wakeup": {"prompt": "completion", "stream_id": None},
        },
    }

    begin_async_delegation_cancellation(session)

    assert running_wakeup_stream_ids(session, {"deleg-1", "deleg-2"}) == ["stream-1"]


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
    assert "delegation_id" not in session.async_delegation_origins["deleg-1"]


def test_dispatch_projects_lifecycle_state_to_its_origin_user_message():
    session = _session()
    session.messages = [
        {"role": "user", "content": "first", "_turn_key": "turn:7"},
        {
            "role": "user",
            "content": "dispatch",
            "_turn_key": "turn:8",
            "_background_task_ids": ["deleg-1"],
        },
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
    assert "_background_task_ids" not in session.messages[1]


def test_dispatch_does_not_project_lifecycle_to_hidden_completion_anchor():
    session = _session()
    session.messages = [
        {"role": "user", "content": "dispatch", "_turn_key": "turn:8"},
        {
            "role": "user",
            "content": "internal completion",
            "_turn_key": "turn:8",
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "async_delegation_completion",
        },
    ]

    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )

    assert "async_delegations" in session.messages[0]
    assert "async_delegations" not in session.messages[1]


def test_batch_dispatch_persists_child_metadata_and_snapshot_counts():
    session = _session()
    session.messages = [{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}]
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
    assert "goals" not in session.async_delegation_origins["deleg-batch"]
    assert session.messages[0]["async_delegations"]["items"]["deleg-batch"]["goals"] == [
        "research model A",
        "research model B",
    ]
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
        dispatched_at=dispatched["dispatched_at"],
    )

    assert event["schema_version"] == 1
    assert event["event_type"] == "background_task_dispatched"
    assert event["event_id"] == "deleg-1:dispatched:1"
    assert event["background_activity_version"] == 1
    assert event["payload"] == {
        "delegation_id": "deleg-1",
        "child_task_count": 1,
        "origin_turn_key": "turn:8",
        "status": "running",
        "wakeup_state": "idle",
        "dispatched_at": dispatched["dispatched_at"],
    }


def test_legacy_top_level_fields_are_migrated_to_the_compact_sidecar():
    session = _session()
    session.messages = [{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}]
    session.async_delegation_origins = {
        "deleg-1": {
            "delegation_id": "deleg-1",
            "turn_key": "turn:8",
            "created_at": 10.0,
            "goals": ["legacy goal"],
            "status": "running",
            "wakeup_state": "idle",
        }
    }

    mark_async_delegation_wakeup(
        session,
        "deleg-1",
        wakeup_state="queued",
        content="ignored",
    )

    record = session.async_delegation_origins["deleg-1"]
    assert record["dispatched_at"] == 10.0
    assert "created_at" not in record
    assert "delegation_id" not in record
    assert "goals" not in record
    assert session.messages[0]["async_delegations"]["items"]["deleg-1"]["goals"] == ["legacy goal"]


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


def test_new_cancel_request_is_idle_after_a_previous_scope_settled():
    session = _session()
    record_async_delegation_dispatch(
        session,
        {"status": "dispatched", "mode": "background", "delegation_id": "deleg-1"},
        turn_key="turn:8",
    )
    begin_async_delegation_cancellation(session, now=10.0)
    mark_async_delegation_completion(
        session,
        "deleg-1",
        wakeup_state="settled",
        content="ignored",
        status="cancelled",
        cancel_state="cancelled",
    )

    assert cancellation_status(session)["state"] == "settled"
    assert begin_async_delegation_cancellation(session, now=11.0) == {
        "state": "idle",
        "delegation_ids": [],
        "requested_at": None,
        "settled_at": None,
    }
    assert cancellation_status(session)["state"] == "settled"


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
