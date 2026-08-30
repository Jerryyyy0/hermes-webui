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


def test_direct_cron_materialization_settles_write_file_artifact(tmp_path, monkeypatch):
    from api.models import Session
    from integration.crons import session_bridge
    from integration.session_manifest.store import load_manifest_records
    import api.models as models
    import integration.session_manifest.store as manifest_store

    _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(manifest_store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(models, "ensure_cron_project", lambda profile=None: "cron-project")
    monkeypatch.setattr(
        "api.session_events.publish_session_list_changed",
        lambda *args, **kwargs: None,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.md"
    artifact.write_text("# report\n", encoding="utf-8")
    messages = [
        {"role": "user", "content": "write the report", "timestamp": 1780461497.0},
        {
            "role": "assistant",
            "content": "",
            "timestamp": 1780461498.0,
            "tool_calls": [{
                "id": "call-write-report",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": str(artifact)}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-write-report",
            "name": "write_file",
            "timestamp": 1780461499.0,
            "content": json.dumps({"resolved_path": str(artifact), "bytes_written": 9}),
        },
        {"role": "assistant", "content": f"Created `{artifact}`.", "timestamp": 1780461500.0},
    ]
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *args, **kwargs: messages)

    sid = "cron_job123_20260603_123817"
    assert session_bridge._materialize_and_settle_cron_session_found(
        {"id": "job123", "name": "Nightly check", "prompt": "Write report"},
        (sid, "", 1780461497.0, "MiniMax-M3"),
        target_profile="default",
        execution_profile="default",
        execution_ended_at=1780461500.0,
        state_db_cwd=str(workspace),
    ) == sid

    session = Session.load(sid)
    assert session is not None
    assert [(row["turn_key"], row["path"], row["source_tool"])
            for row in load_manifest_records(session)] == [
        ("turn:1", "report.md", "write_file"),
    ]


def test_materialized_cron_settlement_reports_persisted(monkeypatch):
    from integration.crons import session_bridge
    from integration.crons.hooks import CronManifestSettlement

    monkeypatch.setattr(
        "integration.crons.hooks.settle_materialized_cron_session",
        lambda _session: CronManifestSettlement(
            "persisted", next_turn_key="turn:2", settled_turn_keys=("turn:1",)
        ),
    )

    assert session_bridge._settle_materialized_cron_session(
        type("Session", (), {"session_id": "cron-ok"})()
    ) == {
        "status": "persisted",
        "session_id": "cron-ok",
        "profile": "",
        "stage": "complete",
        "next_turn_key": "turn:2",
        "settled_turn_keys": ("turn:1",),
    }


def test_materialized_cron_settlement_distinguishes_unsettled_and_failed(monkeypatch):
    from integration.crons import session_bridge
    from integration.crons.hooks import CronManifestSettlement

    session = type("Session", (), {"session_id": "cron-pending"})()
    monkeypatch.setattr(
        "integration.crons.hooks.settle_materialized_cron_session",
        lambda _session: CronManifestSettlement("unsettled", error_stage="execution_prefix"),
    )
    pending = session_bridge._settle_materialized_cron_session(session)
    assert pending["status"] == "unsettled"
    assert pending["stage"] == "execution_prefix"

    monkeypatch.setattr(
        "integration.crons.hooks.settle_materialized_cron_session",
        lambda _session: CronManifestSettlement("failed", error_stage="artifact_decision"),
    )
    failed = session_bridge._settle_materialized_cron_session(session)
    assert failed["status"] == "failed"
    assert failed["stage"] == "artifact_decision"


def test_materialized_cron_settlement_redacts_unexpected_error(monkeypatch):
    from integration.crons import session_bridge

    def raise_error(_session):
        raise RuntimeError("prompt contents and secret should not be returned")

    monkeypatch.setattr(
        "integration.crons.hooks.settle_materialized_cron_session",
        raise_error,
    )

    result = session_bridge._settle_materialized_cron_session(
        type("Session", (), {"session_id": "cron-error"})()
    )

    assert result == {
        "status": "failed",
        "session_id": "cron-error",
        "profile": "",
        "stage": "unexpected",
        "error_type": "RuntimeError",
    }


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
