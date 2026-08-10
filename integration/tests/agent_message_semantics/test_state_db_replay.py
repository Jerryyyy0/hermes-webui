"""Durable context anchors retain original text but not visible-turn meaning."""

import sqlite3

import pytest

from api.models import get_state_db_session_messages
from api.session_manifest import _message_turns
from api.streaming import _drop_synthetic_control_messages


CONTEXT_ANCHOR_KINDS = (
    "length_continuation",
    "codex_incomplete_nudge",
    "intent_ack_continuation",
    "max_iteration_summary_request",
    "todo_snapshot",
    "compaction_summary",
    "compression_no_user_anchor",
    "mcp_reload",
    "model_switch",
    "personality_pivot",
)


@pytest.mark.parametrize("kind", CONTEXT_ANCHOR_KINDS)
def test_state_db_replay_keeps_context_anchor_semantics(tmp_path, monkeypatch, kind):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL,
                active INTEGER,
                api_content TEXT,
                hermes_message_class TEXT,
                hermes_scaffold_kind TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "semantic-session",
                "user",
                "[System: the active model changed]",
                1.0,
                1,
                None,
                "context_anchor",
                kind,
            ),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "semantic-session", "user", "real request", 2.0, 1, None, None, None),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (3, "semantic-session", "assistant", "final answer", 3.0, 1, None, None, None),
        )

    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)
    messages = get_state_db_session_messages("semantic-session")

    assert messages[0]["content"] == "[System: the active model changed]"
    assert messages[0]["_hermes_message_class"] == "context_anchor"
    assert [row["content"] for row in _drop_synthetic_control_messages(messages)] == [
        "real request",
        "final answer",
    ]
    turns = _message_turns(messages)
    assert [(turn["turn_key"], turn["start_msg_idx"], turn["end_msg_idx"]) for turn in turns] == [
        ("turn:1", 1, 2),
    ]
