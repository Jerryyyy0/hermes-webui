"""Regression: stale stream cleanup must not discard pending user turns.

A server restart drops the in-memory STREAMS table. Browser reload then calls
get_session(), which clears stale active_stream_id state. For long conversations
that already have messages, the pending_user_message can be the only durable copy
of the user turn that was submitted just before the restart.
"""

import api.config as config
import api.models as models
from api.models import Session, get_session


def test_stale_stream_cleanup_recovers_pending_turn_on_non_empty_session(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    config.STREAMS.clear()

    s = Session(
        session_id="stale_stream_nonempty",
        title="Existing long chat",
        messages=[
            {"role": "user", "content": "previous prompt", "timestamp": 100},
            {"role": "assistant", "content": "previous answer", "timestamp": 101},
        ],
    )
    s.active_stream_id = "dead_stream"
    s.pending_user_message = "new prompt that must survive restart"
    s.pending_attachments = [{"name": "note.txt", "path": "/tmp/note.txt"}]
    s.pending_started_at = 123
    s.save()

    recovered = get_session("stale_stream_nonempty")

    assert recovered.active_stream_id is None
    assert recovered.pending_user_message is None
    assert any(
        msg.get("role") == "user"
        and msg.get("content") == "new prompt that must survive restart"
        and msg.get("_recovered") is True
        for msg in recovered.messages
    )
    assert any(
        msg.get("role") == "assistant" and msg.get("_error") is True
        for msg in recovered.messages
    )


def test_stale_stream_cleanup_does_not_duplicate_checkpointed_pending_turn(tmp_path, monkeypatch):
    """A failed turn may already have a user anchor before tool activity."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    config.STREAMS.clear()

    attachment = {"name": "route.md", "path": "/tmp/route.md"}
    s = Session(
        session_id="stale_stream_checkpointed",
        title="Interrupted tool turn",
        messages=[
            {"role": "user", "content": "plan the route", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "Checking options"},
            {"role": "tool", "content": "partial tool output"},
        ],
        context_messages=[
            {
                "role": "user",
                "content": "[Workspace::v1: /tmp/workspace]\nplan the route",
                "_turn_key": "turn:1",
            },
            {"role": "assistant", "content": "Checking options"},
            {"role": "tool", "content": "partial tool output"},
        ],
    )
    s.active_stream_id = "dead_stream"
    s.pending_user_message = "plan the route"
    s.pending_attachments = [attachment]
    s.pending_started_at = 123
    s.pending_turn_key = "turn:1"
    s.save()

    recovered = get_session("stale_stream_checkpointed")

    display_users = [m for m in recovered.messages if m.get("role") == "user"]
    context_users = [m for m in recovered.context_messages if m.get("role") == "user"]
    assert len(display_users) == 1
    assert len(context_users) == 1
    assert display_users[0]["_turn_key"] == "turn:1"
    assert context_users[0]["_turn_key"] == "turn:1"
    assert display_users[0]["attachments"] == [attachment]
    assert context_users[0]["attachments"] == [attachment]
    assert recovered.pending_user_message is None
    assert any(m.get("role") == "assistant" and m.get("_error") for m in recovered.messages)
