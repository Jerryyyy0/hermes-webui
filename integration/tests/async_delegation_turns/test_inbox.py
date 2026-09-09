from types import SimpleNamespace
import sys
import types

from api.models import Session
from integration.async_delegation_turns import inbox


def test_sidecar_scan_uses_actual_session_filename_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("api.config.SESSION_DIR", tmp_path)
    (tmp_path / "120d7d229c1f.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_index.json").write_text("{}", encoding="utf-8")
    (tmp_path / "120d7d229c1f.json.bak").write_text("{}", encoding="utf-8")

    assert inbox._sidecar_session_ids() == ["120d7d229c1f"]


def test_backend_probe_uses_gateway_module_helper(monkeypatch):
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.gateway_chat.webui_gateway_chat_enabled", lambda _config: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    assert inbox._backend_supported(SimpleNamespace()) is True


def test_startup_recovery_settles_running_wakeup_after_terminal_save(monkeypatch):
    session = SimpleNamespace(
        session_id="session-1",
        pending_user_source=None,
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_turn_key=None,
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:8",
                "status": "completed",
                "wakeup_state": "running",
                "wakeup": {"prompt": "completion", "stream_id": "stream-1"},
            }
        },
        saves=0,
    )
    session.save = lambda *_args, **_kwargs: setattr(session, "saves", session.saves + 1)
    monkeypatch.setattr(inbox, "_sidecar_session_ids", lambda: ["session-1"])
    monkeypatch.setattr(Session, "load", classmethod(lambda _cls, _sid: session))
    monkeypatch.setattr(inbox, "notify", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "api.run_journal.latest_run_summary",
        lambda *_args, **_kwargs: {"terminal": True, "terminal_state": "completed"},
    )

    assert inbox.recover() == 0
    assert session.saves == 1
    assert session.async_delegation_origins["deleg-1"]["wakeup_state"] == "settled"
    assert "wakeup" not in session.async_delegation_origins["deleg-1"]


def test_ack_pending_recovery_marks_delivered_row_acknowledged(monkeypatch):
    session = SimpleNamespace(
        session_id="session-ack",
        messages=[],
        async_delegation_origins={
            "deleg-1": {
                "wakeup_state": "queued",
                "wakeup": {"prompt": "completion", "error_code": "agent_ack_pending"},
            }
        },
    )
    fake_delivery = types.ModuleType("tools.async_delegation")
    fake_delivery.get_durable_delegation = lambda _delegation_id: {"delivery_state": "delivered"}
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_delivery)
    monkeypatch.setattr(
        "integration.async_delegation_turns.mark_completion_ack",
        lambda target, delegation_id, **_kwargs: target.async_delegation_origins[delegation_id]["wakeup"].update(
            error_code=None
        ),
    )

    assert inbox._reconcile_ack_pending(session)
    assert session.async_delegation_origins["deleg-1"]["wakeup"]["error_code"] is None


def test_ack_pending_recovery_treats_confirmed_missing_row_as_unknown(monkeypatch):
    """A pruned Agent outbox row must not strand an already-owned WebUI inbox."""
    session = SimpleNamespace(
        session_id="session-ack-missing",
        messages=[],
        async_delegation_origins={
            "deleg-1": {
                "wakeup_state": "queued",
                "wakeup": {"prompt": "completion", "error_code": "agent_ack_pending"},
            }
        },
    )
    fake_delivery = types.ModuleType("tools.async_delegation")
    fake_delivery.get_durable_delegation = lambda _delegation_id: None
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_delivery)
    monkeypatch.setattr(
        "integration.async_delegation_turns.mark_completion_ack",
        lambda target, delegation_id, **_kwargs: target.async_delegation_origins[delegation_id]["wakeup"].update(
            error_code="agent_ack_unknown"
        ),
    )

    assert inbox._reconcile_ack_pending(session)
    assert (
        session.async_delegation_origins["deleg-1"]["wakeup"]["error_code"]
        == "agent_ack_unknown"
    )


def test_ack_pending_recovery_marks_legacy_no_readback_as_unknown(monkeypatch):
    session = SimpleNamespace(
        session_id="session-ack-legacy",
        messages=[],
        async_delegation_origins={
            "deleg-1": {
                "wakeup_state": "queued",
                "wakeup": {"prompt": "completion", "error_code": "agent_ack_pending"},
            }
        },
    )
    fake_delivery = types.ModuleType("tools.async_delegation")
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_delivery)
    monkeypatch.setattr(
        "integration.async_delegation_turns.mark_completion_ack",
        lambda target, delegation_id, **_kwargs: target.async_delegation_origins[delegation_id]["wakeup"].update(
            error_code="agent_ack_unknown"
        ),
    )

    assert inbox._reconcile_ack_pending(session)
    assert (
        session.async_delegation_origins["deleg-1"]["wakeup"]["error_code"]
        == "agent_ack_unknown"
    )


def test_drain_retries_ack_pending_without_starting_agent(monkeypatch):
    """Readback outages remain scheduler candidates even without Agent redelivery."""
    session = SimpleNamespace(
        session_id="session-ack-retry",
        archived=False,
        active_stream_id=None,
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:1",
                "wakeup_state": "queued",
                "cancel_state": "none",
                "wakeup": {"prompt": "completion", "error_code": "agent_ack_pending"},
            }
        },
    )
    notifications = []
    starts = []
    monkeypatch.setattr("api.config._get_session_agent_lock", lambda _sid: _NoopLock())
    monkeypatch.setattr(inbox, "_load_session", lambda _sid: session)
    monkeypatch.setattr(inbox, "_reconcile_ack_pending", lambda _session: False)
    monkeypatch.setattr(inbox, "notify", lambda sid, **kwargs: notifications.append((sid, kwargs)) or True)
    monkeypatch.setattr("api.routes.start_session_turn", lambda *args, **kwargs: starts.append((args, kwargs)))

    inbox._drain_one("session-ack-retry")

    assert starts == []
    assert notifications == [("session-ack-retry", {"delay": 5.0})]


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_scheduler_passes_hidden_completion_metadata(monkeypatch):
    from integration.agent_message_semantics.projection import drop_non_display_messages

    session = SimpleNamespace(archived=False, active_stream_id=None)
    monkeypatch.setattr(inbox, "_load_session", lambda sid: session)
    monkeypatch.setattr(inbox, "_reconcile_ack_pending", lambda s: False)
    monkeypatch.setattr(inbox, "_backend_supported", lambda s: True)
    monkeypatch.setattr("integration.pending_chat_turns.pending_turns", lambda s: [])
    monkeypatch.setattr("integration.async_delegation_turns.select_next_queued_wakeup",
                        lambda s: ("deleg-test", {"turn_key": "turn:1", "wakeup": {"prompt": "completion"}}))
    starts = []
    def start(sid, prompt, **kwargs):
        starts.append(kwargs)
        return {"stream_id": "stream-test"}
    monkeypatch.setattr("api.routes.start_session_turn", start)
    inbox._drain_one("session-test")
    metadata = starts[0]["user_message_metadata"]
    assert metadata == {"_hermes_message_class": "context_anchor",
                        "_hermes_scaffold_kind": "async_delegation_completion"}
    human = {"role": "user", "content": "completion"}
    reply = {"role": "assistant", "content": "result"}
    tool = {"role": "tool", "content": "written"}
    assert drop_non_display_messages([
        human, {"role": "user", "content": "completion", **metadata}, reply, tool,
    ]) == [human, reply, tool]
