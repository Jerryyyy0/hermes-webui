from __future__ import annotations

from collections import OrderedDict
import queue
import sys
import types

from api.models import Session


def test_successful_async_worker_commits_event_then_settles_sidecar(tmp_path, monkeypatch):
    from api import background_process, config, models, streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)

    prompt = "[ASYNC DELEGATION BATCH COMPLETE - deleg-1]"
    stream_id = "stream-async-1"
    session = Session(
        session_id="session-async-stream",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            {"role": "user", "content": "dispatch", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "dispatched", "_turn_key": "turn:1"},
        ],
        context_messages=[
            {"role": "user", "content": "dispatch", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "dispatched", "_turn_key": "turn:1"},
        ],
        active_stream_id=stream_id,
        active_stream_generation=2,
        control_generation=2,
        pending_user_message=prompt,
        pending_attachments=[],
        pending_user_source="async_delegation_wakeup",
        pending_turn_key="turn:1",
        async_delegation_activity_version=2,
        async_delegation_origins={
            "deleg-1": {
                "turn_key": "turn:1",
                "status": "completed",
                "wakeup_state": "running",
                "activity_version": 2,
                "wakeup": {
                    "prompt": prompt,
                    "stream_id": stream_id,
                    "start_attempts": 1,
                    "error_code": None,
                },
            }
        },
    )
    session.save(touch_updated_at=False)
    models.SESSIONS[session.session_id] = session

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = session.session_id
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            if self.stream_delta_callback:
                self.stream_delta_callback("async final")
                self.stream_delta_callback(None)
            return {
                "completed": True,
                "final_response": "async final",
                "messages": history + [
                    {
                        "role": "user",
                        "content": prompt,
                        "_hermes_message_class": "context_anchor",
                        "_hermes_scaffold_kind": "async_delegation_completion",
                    },
                    {"role": "assistant", "content": "async final"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_runtime = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime.resolve_runtime_provider = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)

    events = queue.Queue()
    config.STREAMS[stream_id] = events
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(streaming, "_run_background_title_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda *_args, **_kwargs: {
            "status": "persisted",
            "stage": "stored",
            "turn_key": "turn:1",
        },
    )
    session_events = []
    monkeypatch.setattr(
        background_process,
        "emit_session_channel_event",
        lambda sid, event, payload: session_events.append((sid, event, payload)) or 1,
    )

    streaming._run_agent_streaming(
        session_id=session.session_id,
        msg_text=prompt,
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
        model_provider="test-provider",
        stream_turn_key="turn:1",
        user_message_metadata={
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "async_delegation_completion",
        },
        async_delegation_id="deleg-1",
    )

    ordered_events = [item[0] for item in list(events.queue)]
    assert ordered_events.index("done") < ordered_events.index("async_turn_committed")
    assert session_events[0][0:2] == (session.session_id, "async_turn_committed")
    saved = Session.load(session.session_id)
    assert saved is not None
    record = saved.async_delegation_origins["deleg-1"]
    assert record["wakeup_state"] == "settled"
    assert "wakeup" not in record
    assert saved.messages[0]["role"] == "user"
    assert saved.messages[-1]["content"] == "async final"
    assert saved.messages[-1]["_turn_key"] == "turn:1"
    assert saved.messages[-1]["delegation_id"] == "deleg-1"
