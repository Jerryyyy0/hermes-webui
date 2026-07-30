"""Regression coverage for database-primary cron execution history."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


def _make_state_db(path: Path, rows: list[tuple], messages: list[tuple] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                end_reason TEXT,
                model TEXT,
                message_count INTEGER,
                tool_call_count INTEGER,
                api_call_count INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO sessions (
                id, title, source, started_at, ended_at, end_reason,
                model, message_count, tool_call_count, api_call_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            messages or [],
        )
        conn.commit()


def _make_current_state_db(path: Path) -> None:
    """Create the Agent-owned schema used by the backfill write path."""
    import cron.jobs

    agent_root = str(Path(cron.jobs.__file__).resolve().parent.parent)
    if agent_root not in sys.path:
        sys.path.insert(0, agent_root)
    from hermes_state import SessionDB

    store = SessionDB(db_path=path)
    store.close()


class _Handler:
    def __init__(self):
        self.payload = None
        self.status = None


def _install_json_capture(monkeypatch):
    def capture(handler, payload, status=200, **_kwargs):
        handler.payload = payload
        handler.status = status
        return True

    monkeypatch.setattr("api.routes.j", capture)


def test_history_reads_legacy_job_session_from_owner_profile(monkeypatch, tmp_path):
    owner_home = tmp_path / "abc"
    default_home = tmp_path / "default"
    job_id = "f9cbbc335190"
    owner_session = f"cron_{job_id}_20260714_151052"
    default_session = f"cron_{job_id}_20260714_151000"
    _make_state_db(
        owner_home / "state.db",
        [(owner_session, "站立提醒", "cron", 100.0, 112.0, "cron_complete", "owner", 2, 0, 1)],
        [("owner-message", owner_session, "user", "stand", 100.0)],
    )
    _make_state_db(
        default_home / "state.db",
        [(default_session, "Wrong profile", "cron", 200.0, 202.0, "cron_complete", "default", 1, 0, 1)],
    )
    output_dir = owner_home / "cron" / "output" / job_id
    output_dir.mkdir(parents=True)
    output = output_dir / "2026-07-14_15-11-12.md"
    output.write_text("# Response\n\nDone", encoding="utf-8")
    os.utime(output, (112.0, 112.0))

    import cron.jobs
    import api.routes

    monkeypatch.setattr(cron.jobs, "OUTPUT_DIR", owner_home / "cron" / "output")
    monkeypatch.setattr(cron.jobs, "get_job", lambda value: {"id": value, "name": "站立提醒", "profile": None})
    monkeypatch.setattr(api.routes, "_execution_home_for_cron_session_lookup", lambda job, owner: owner_home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        "integration.crons.session_bridge.materialize_cron_session_run",
        lambda *_args, **_kwargs: owner_session,
    )
    _install_json_capture(monkeypatch)

    handler = _Handler()
    api.routes._handle_cron_history(
        handler,
        SimpleNamespace(query=f"job_id={job_id}&profile=abc&limit=50"),
    )

    assert handler.status == 200
    assert handler.payload["profile"] == "abc"
    assert handler.payload["total"] == 1
    run = handler.payload["runs"][0]
    assert run["session_id"] == owner_session
    assert run["model"] == "owner"
    assert run["output_filename"] == output.name


def test_history_keeps_database_only_and_artifact_only_runs(monkeypatch, tmp_path):
    home = tmp_path / "abc"
    job_id = "one-shot"
    session_id = f"cron_{job_id}_20260714_151052"
    _make_state_db(
        home / "state.db",
        [(session_id, "One shot", "cron", 100.0, 110.0, "cron_complete", "model", 2, 1, 1)],
    )
    artifact_dir = home / "cron" / "output" / job_id
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "orphan.md"
    artifact.write_text("# Response\n\nOld script output", encoding="utf-8")
    os.utime(artifact, (1000.0, 1000.0))

    import cron.jobs
    import api.routes

    monkeypatch.setattr(cron.jobs, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(cron.jobs, "get_job", lambda value: {"id": value, "profile": None})
    monkeypatch.setattr(api.routes, "_execution_home_for_cron_session_lookup", lambda job, owner: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "abc")
    monkeypatch.setattr(
        "integration.crons.session_bridge.materialize_cron_session_run",
        lambda *_args, **_kwargs: session_id,
    )
    _install_json_capture(monkeypatch)

    handler = _Handler()
    api.routes._handle_cron_history(handler, SimpleNamespace(query=f"job_id={job_id}&profile=abc"))

    assert handler.payload["total"] == 2
    database_run = next(run for run in handler.payload["runs"] if run["session_id"] == session_id)
    artifact_run = next(run for run in handler.payload["runs"] if run["session_id"] is None)
    assert database_run["output_filename"] is None
    assert artifact_run["output_filename"] == artifact.name


def test_history_backfills_output_only_run_as_cron_session(monkeypatch, tmp_path):
    home = tmp_path / "abc"
    job_id = "eb1aacc4beb1"
    home.mkdir()
    import cron.jobs
    import api.routes

    _make_current_state_db(home / "state.db")
    output_dir = home / "cron" / "output" / job_id
    output_dir.mkdir(parents=True)
    output = output_dir / "2026-07-30_17-20-49.md"
    output.write_text(
        "# Cron Job: 每日简报\n\n## Response\n\n今日简报已完成。",
        encoding="utf-8",
    )
    completed_at = 1785403249.8464258
    os.utime(output, (completed_at, completed_at))

    monkeypatch.setattr(cron.jobs, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(
        cron.jobs,
        "get_job",
        lambda value: {
            "id": value,
            "name": "每日简报",
            "prompt": "生成每日简报",
            "profile": None,
        },
    )
    monkeypatch.setattr(api.routes, "_execution_home_for_cron_session_lookup", lambda job, owner: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "abc")
    monkeypatch.setattr(
        "integration.crons.session_bridge.materialize_cron_session_run",
        lambda *_args, **_kwargs: None,
    )
    _install_json_capture(monkeypatch)

    handler = _Handler()
    api.routes._handle_cron_history(
        handler,
        SimpleNamespace(query=f"job_id={job_id}&profile=abc&limit=50"),
    )

    expected_sid = f"cron_{job_id}_20260730_172049"
    assert handler.payload["total"] == 1
    assert handler.payload["runs"][0]["session_id"] == expected_sid
    assert handler.payload["runs"][0]["ended_at"] == completed_at
    assert handler.payload["runs"][0]["end_reason"] == "cron_complete"

    with closing(sqlite3.connect(str(home / "state.db"))) as conn:
        session = conn.execute(
            "SELECT source, title, message_count, ended_at, end_reason FROM sessions WHERE id = ?",
            (expected_sid,),
        ).fetchone()
        messages = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (expected_sid,),
        ).fetchall()
    assert session == ("cron", "每日简报", 2, completed_at, "cron_complete")
    assert messages == [("user", "生成每日简报"), ("assistant", "今日简报已完成。")]


def test_history_backfill_is_idempotent(monkeypatch, tmp_path):
    home = tmp_path / "abc"
    job_id = "backfill-repeat"
    home.mkdir()
    import cron.jobs
    import api.routes

    _make_current_state_db(home / "state.db")
    output_dir = home / "cron" / "output" / job_id
    output_dir.mkdir(parents=True)
    output = output_dir / "2026-07-30_17-20-49.md"
    output.write_text("# Response\n\nDone", encoding="utf-8")
    os.utime(output, (1785403249.0, 1785403249.0))

    monkeypatch.setattr(cron.jobs, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(
        cron.jobs,
        "get_job",
        lambda value: {"id": value, "name": "Repeat", "prompt": "Run", "profile": None},
    )
    monkeypatch.setattr(api.routes, "_execution_home_for_cron_session_lookup", lambda job, owner: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "abc")
    monkeypatch.setattr(
        "integration.crons.session_bridge.materialize_cron_session_run",
        lambda *_args, **_kwargs: None,
    )
    _install_json_capture(monkeypatch)

    for _ in range(2):
        api.routes._handle_cron_history(
            _Handler(),
            SimpleNamespace(query=f"job_id={job_id}&profile=abc&limit=50"),
        )

    with closing(sqlite3.connect(str(home / "state.db"))) as conn:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source = 'cron'"
        ).fetchone()[0]
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert session_count == 1
    assert message_count == 2


def test_history_paginates_merged_database_and_artifact_runs(monkeypatch, tmp_path):
    home = tmp_path / "abc"
    job_id = "paged"
    first = f"cron_{job_id}_20260714_151052"
    second = f"cron_{job_id}_20260714_151000"
    _make_state_db(
        home / "state.db",
        [
            (first, "New", "cron", 200.0, 210.0, "cron_complete", "model", 1, 0, 1),
            (second, "Old", "cron", 100.0, 110.0, "cron_complete", "model", 1, 0, 1),
        ],
    )

    import cron.jobs
    import api.routes

    monkeypatch.setattr(cron.jobs, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(cron.jobs, "get_job", lambda value: {"id": value, "profile": None})
    monkeypatch.setattr(api.routes, "_execution_home_for_cron_session_lookup", lambda job, owner: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "abc")
    monkeypatch.setattr(
        "integration.crons.session_bridge.materialize_cron_session_run",
        lambda *_args, **_kwargs: None,
    )
    _install_json_capture(monkeypatch)

    handler = _Handler()
    api.routes._handle_cron_history(
        handler,
        SimpleNamespace(query=f"job_id={job_id}&profile=abc&offset=1&limit=1"),
    )

    assert handler.payload["total"] == 2
    assert [run["session_id"] for run in handler.payload["runs"]] == [second]
