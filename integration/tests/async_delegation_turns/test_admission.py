from types import SimpleNamespace

import pytest

from api import config, routes


def test_cancel_before_async_admission_leaves_core_pending_untouched(monkeypatch, tmp_path):
    config.ACTIVE_RUNS.clear()
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = SimpleNamespace(
        session_id="async-admission-cancelled",
        active_stream_id=None,
        active_stream_generation=None,
        control_generation=4,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        pending_turn_key=None,
        pending_next_turns=[],
        messages=[{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}],
        title="Existing",
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        source_tag="webui",
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:8",
                "status": "completed",
                "wakeup_state": "queued",
                "cancel_state": "requested",
                "wakeup": {"prompt": "completion", "stream_id": None},
            }
        },
        saves=0,
    )

    def save(*_args, **_kwargs):
        session.saves += 1

    session.save = save
    monkeypatch.setattr(routes, "_validate_start_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_reload_session_for_locked_start", lambda current: current)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)

    result = routes._start_chat_stream_for_session(
        session,
        msg="completion",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        source="async_delegation_wakeup",
        turn_key_override="turn:8",
        server_turn_metadata={"delegation_id": "deleg-1"},
    )

    assert result["_status"] == 409
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.control_generation == 4
    assert session.saves == 0


def test_async_admission_saves_core_and_wakeup_state_once(monkeypatch, tmp_path):
    config.ACTIVE_RUNS.clear()
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = SimpleNamespace(
        session_id="async-admission-once",
        profile="default",
        active_stream_id=None,
        active_stream_generation=None,
        control_generation=4,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        pending_turn_key=None,
        pending_next_turns=[],
        messages=[{"role": "user", "content": "dispatch", "_turn_key": "turn:8"}],
        title="Existing",
        workspace=str(tmp_path),
        workspace_mode="external",
        model="test-model",
        model_provider=None,
        source_tag="webui",
        async_delegation_activity_version=1,
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:8",
                "status": "completed",
                "wakeup_state": "queued",
                "cancel_state": "none",
                "wakeup": {
                    "prompt": "completion",
                    "stream_id": None,
                    "start_attempts": 0,
                    "error_code": None,
                },
            }
        },
        saves=0,
    )

    def save(*_args, **_kwargs):
        session.saves += 1

    session.save = save

    lifecycle_order = []

    class _Thread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            lifecycle_order.append("worker_start")
            return None

    monkeypatch.setattr(routes, "_validate_start_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_reload_session_for_locked_start", lambda current: current)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes.threading, "Thread", _Thread)
    monkeypatch.setattr(
        "integration.async_delegation_turns.publish_server_turn_started",
        lambda *_args, **_kwargs: lifecycle_order.append("server_turn_started"),
    )
    monkeypatch.setattr(
        "api.turn_journal.append_turn_journal_event",
        lambda *_args, **_kwargs: {},
    )

    result = routes._start_chat_stream_for_session(
        session,
        msg="completion",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        external_runtime_owned=False,
        source="async_delegation_wakeup",
        turn_key_override="turn:8",
        server_turn_metadata={"delegation_id": "deleg-1"},
    )

    record = session.async_delegation_origins["deleg-1"]
    assert result["stream_id"] == session.active_stream_id
    assert session.saves == 1
    assert session.pending_user_source == "async_delegation_wakeup"
    assert session.pending_turn_key == "turn:8"
    assert record["wakeup_state"] == "running"
    assert record["wakeup"]["stream_id"] == result["stream_id"]
    assert record["wakeup"]["start_attempts"] == 1
    # The OS thread exists first, but its async worker target is held behind a
    # gate until server_turn_started has been published.
    assert lifecycle_order == ["worker_start", "server_turn_started"]
    config.STREAMS.pop(result["stream_id"], None)


def test_worker_dispatch_failure_rolls_back_only_admitted_generation(monkeypatch, tmp_path):
    config.ACTIVE_RUNS.clear()
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = SimpleNamespace(
        session_id="async-admission-rollback",
        profile="default",
        active_stream_id=None,
        active_stream_generation=None,
        control_generation=1,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        pending_turn_key=None,
        pending_next_turns=[],
        messages=[{"role": "user", "content": "dispatch", "_turn_key": "turn:2"}],
        title="Existing",
        workspace=str(tmp_path),
        workspace_mode="external",
        model="test-model",
        model_provider=None,
        source_tag="webui",
        async_delegation_activity_version=1,
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:2",
                "status": "completed",
                "wakeup_state": "queued",
                "cancel_state": "none",
                "wakeup": {"prompt": "completion", "start_attempts": 0},
            }
        },
        saves=0,
    )
    session.save = lambda *_args, **_kwargs: setattr(session, "saves", session.saves + 1)
    published = []

    class _FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(routes, "_validate_start_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_reload_session_for_locked_start", lambda current: current)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes.threading, "Thread", _FailingThread)
    monkeypatch.setattr(
        "integration.async_delegation_turns.publish_server_turn_started",
        lambda *_args, **_kwargs: published.append(True),
    )
    monkeypatch.setattr(routes.Session, "load", lambda _sid: session)
    monkeypatch.setattr("api.turn_journal.append_turn_journal_event", lambda *_args, **_kwargs: {})

    with pytest.raises(RuntimeError, match="thread unavailable"):
        routes._start_chat_stream_for_session(
            session,
            msg="completion",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
            external_runtime_owned=False,
            source="async_delegation_wakeup",
            turn_key_override="turn:2",
            server_turn_metadata={"delegation_id": "deleg-1"},
        )

    record = session.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "queued"
    assert record["wakeup"]["stream_id"] is None
    assert record["wakeup"]["error_code"] == "worker_dispatch_failed"
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert config.STREAMS == {}
    assert published == []
