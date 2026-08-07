from pathlib import Path

from api import streaming


def test_chat_start_appends_submitted_turn_journal_before_worker_thread_start():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    save_idx = src.index("_prepare_chat_start_session_for_stream(")
    append_idx = src.index("append_turn_journal_event(", save_idx)
    thread_idx = src.index("threading.Thread(", append_idx)

    assert save_idx < append_idx < thread_idx
    assert '"event": "submitted"' in src[append_idx:thread_idx]
    assert '"role": "user"' in src[append_idx:thread_idx]


def test_chat_start_writes_turn_journal_after_session_lock_and_handles_failure():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    lock_idx = src.index("with session_lock:")
    append_idx = src.index("append_turn_journal_event(", lock_idx)
    stream_registration_idx = src.index("STREAMS[stream_id] = stream", append_idx)
    lock_block = src[lock_idx:append_idx]
    append_block = src[append_idx:stream_registration_idx]

    assert "append_turn_journal_event(" not in lock_block
    assert "except Exception:" in append_block
    assert "Failed to append submitted turn journal event" in append_block


def test_provider_error_terminal_records_interrupted_event(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        streaming,
        "append_turn_journal_event_for_stream",
        lambda session_id, stream_id, event: recorded.append((session_id, stream_id, event)),
    )

    streaming._append_interrupted_turn_journal_event(
        "session-1",
        "stream-1",
        reason="provider_error",
    )

    assert len(recorded) == 1
    session_id, stream_id, event = recorded[0]
    assert (session_id, stream_id) == ("session-1", "stream-1")
    assert event["event"] == "interrupted"
    assert event["reason"] == "provider_error"
    assert isinstance(event["created_at"], float)
