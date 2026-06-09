"""Regression tests for cron session materialization metadata."""

import json
import sqlite3


def _isolate_session_store(tmp_path, monkeypatch):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return session_dir


def test_cron_materialize_preserves_state_db_model(tmp_path, monkeypatch):
    from integration.crons import session_bridge
    import api.models as models

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "ensure_cron_project", lambda profile=None: "cron-project")
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *a, **k: [])
    monkeypatch.setattr(
        "api.session_events.publish_session_list_changed",
        lambda *a, **k: None,
    )

    sid = "cron_job123_20260603_123816"
    result = session_bridge._materialize_cron_session_found(
        {"id": "job123", "name": "Nightly check", "prompt": "Check status"},
        (sid, "", 1780461497.0, "MiniMax-M3"),
        target_profile="default",
        execution_profile="default",
        fallback_output="## Response\nAll good",
        run_mtime=1780461500.0,
    )

    assert result == sid
    payload = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert payload["model"] == "MiniMax-M3"
    assert payload["source_tag"] == "cron"
    assert payload["is_cli_session"] is False


def test_cron_materialize_repairs_existing_unknown_model(tmp_path, monkeypatch):
    from integration.crons import session_bridge
    import api.models as models

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "ensure_cron_project", lambda profile=None: "cron-project")
    monkeypatch.setattr(
        "api.session_events.publish_session_list_changed",
        lambda *a, **k: None,
    )

    sid = "cron_job123_20260603_123816"
    existing = models.Session(
        session_id=sid,
        title="Nightly check",
        workspace=str(tmp_path),
        model="unknown",
        messages=[{"role": "assistant", "content": "old", "timestamp": 1780461500.0}],
        profile="default",
    )
    existing.project_id = "cron-project"
    existing.is_cli_session = False
    existing.source_tag = "cron"
    existing.save(touch_updated_at=False)

    result = session_bridge._materialize_cron_session_found(
        {"id": "job123", "name": "Nightly check"},
        (sid, "", 1780461497.0, "MiniMax-M3"),
        target_profile="default",
        execution_profile="default",
    )

    assert result == sid
    payload = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert payload["model"] == "MiniMax-M3"
    assert payload["messages"] == existing.messages


def test_cron_session_candidate_includes_model(tmp_path):
    from integration.crons import session_bridge

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                model TEXT,
                message_count INTEGER,
                started_at REAL,
                source TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cron_job123_20260603_123816", None, "MiniMax-M3", 0, 1780461497.0, "cron"),
        )
        conn.commit()

        found = session_bridge._select_cron_session_for_run(conn, "job123")
    finally:
        conn.close()

    assert found == ("cron_job123_20260603_123816", "", 1780461497.0, "MiniMax-M3")
