"""Tests for cron session materialize and sidebar visibility."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    home = tmp_path / "profile_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_HOME", str(home))

    state_dir = Path(os.environ["HERMES_WEBUI_STATE_DIR"])
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    import api.config as webui_config
    import api.models as models

    monkeypatch.setattr(webui_config, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(webui_config, "STATE_DIR", state_dir)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    db = home / "state.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                started_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                role TEXT,
                content TEXT,
                timestamp REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("cron_job1_1700000000", "Cron run", "cron", 1700000000.0),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            ("m1", "user", "hello", 1700000000.0),
        )
        conn.commit()

    return {"home": home, "db": db}


def test_cron_sessions_visible_only_materialized(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from integration.crons.session_bridge import cron_sessions_visible_in_sidebar

    assert cron_sessions_visible_in_sidebar({"source_tag": "cron", "is_cli_session": False}) is True
    assert cron_sessions_visible_in_sidebar({"source_tag": "cron", "is_cli_session": True}) is False


def test_hide_sidebar_integration_visible(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from api.models import _hide_from_default_sidebar

    assert _hide_from_default_sidebar({"session_id": "cron_x", "source_tag": "cron", "is_cli_session": False}) is False
    assert _hide_from_default_sidebar({"session_id": "cron_x", "source_tag": "cron", "is_cli_session": True}) is True


def test_materialize_imports_session(cron_env, monkeypatch):
    pytest.importorskip("cron.jobs")
    job = {"id": "job1", "name": "Nightly", "profile": ""}
    owner = "default"

    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "hi"}]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session

            sid = materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
            )
    assert sid == "cron_job1_1700000000"

    from api.models import Session

    meta = Session.load_metadata_only(sid)
    assert meta is not None
    assert meta.profile == owner or getattr(meta, "profile", None) in (owner, None)
    assert meta.is_cli_session is False
    assert meta.source_tag == "cron"


def test_materialize_selects_session_for_run_mtime(cron_env, monkeypatch):
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("cron_job1_1700000100", "Cron run 2", "cron", 1700000100.0),
        )
        conn.commit()

    job = {"id": "job1", "name": "Nightly", "profile": ""}
    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "hi"}]):
        from integration.crons.session_bridge import materialize_cron_session

        sid = materialize_cron_session(
            job,
            owner_profile="default",
            execution_home=cron_env["home"],
            run_mtime=1700000005.0,
        )

    assert sid == "cron_job1_1700000000"


def test_batch_materialize_maps_history_runs_by_mtime(cron_env, monkeypatch):
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("cron_job1_1700000100", "Cron run 2", "cron", 1700000100.0),
        )
        conn.commit()

    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    owner = "default"
    with patch("api.models.get_state_db_session_messages", return_value=[]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_sessions_for_runs

            session_ids = materialize_cron_sessions_for_runs(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
                runs=[
                    {
                        "filename": "first.md",
                        "run_mtime": 1700000005.0,
                        "fallback_output": "## Response\n\nfirst output",
                    },
                    {
                        "filename": "second.md",
                        "run_mtime": 1700000105.0,
                        "fallback_output": "## Response\n\nsecond output",
                    },
                ],
            )

    assert session_ids == {
        "first.md": "cron_job1_1700000000",
        "second.md": "cron_job1_1700000100",
    }

    from api.models import Session

    first = Session.load("cron_job1_1700000000")
    second = Session.load("cron_job1_1700000100")
    assert first.messages[1]["content"] == "first output"
    assert second.messages[1]["content"] == "second output"


def test_build_cron_fallback_messages_prompt_only_user():
    from integration.crons.session_bridge import build_cron_fallback_messages

    msgs = build_cron_fallback_messages(
        {"prompt": "Summarize news"},
        "## Response\n\nDone.",
        run_mtime=1000.0,
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Summarize news"
    assert msgs[0]["source"] == "cron_fallback"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "Done."
    assert msgs[1]["source"] == "cron_fallback"


def test_build_cron_fallback_messages_empty_prompt():
    from integration.crons.session_bridge import build_cron_fallback_messages

    msgs = build_cron_fallback_messages({"prompt": ""}, "no heading body", run_mtime=100.0)
    assert msgs[0]["content"] == ""


def test_materialize_uses_fallback_when_state_db_messages_empty(cron_env, monkeypatch, tmp_path):
    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    owner = "default"
    output_md = "**Model:** test\n\n## Response\n\nHello from cron"

    with patch("api.models.get_state_db_session_messages", return_value=[]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session

            sid = materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
                fallback_output=output_md,
                run_mtime=1700000001.0,
            )

    assert sid == "cron_job1_1700000000"
    from api.models import Session

    full = Session.load(sid)
    assert full is not None
    assert len(full.messages) == 2
    assert full.messages[0]["content"] == "run nightly"
    assert full.messages[1]["content"] == "Hello from cron"
    assert full.messages[0].get("source") == "cron_fallback"


def test_materialize_does_not_overwrite_existing_messages(cron_env, monkeypatch):
    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "new prompt"}
    owner = "default"
    real_msgs = [{"role": "user", "content": "real", "timestamp": 1.0}]

    with patch("api.models.get_state_db_session_messages", return_value=real_msgs):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session

            sid = materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
                fallback_output="## Response\n\nshould not replace",
            )

    from api.models import Session

    full = Session.load(sid)
    assert len(full.messages) == 1
    assert full.messages[0]["content"] == "real"


def test_ensure_cron_project_explicit_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    projects_file = tmp_path / "state" / "projects.json"
    projects_file.parent.mkdir(parents=True, exist_ok=True)
    projects_file.write_text("[]", encoding="utf-8")

    from api import models

    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    pid = models.ensure_cron_project(profile="alice")
    projects = json.loads(projects_file.read_text(encoding="utf-8"))
    row = next(p for p in projects if p["project_id"] == pid)
    assert row["profile"] == "alice"
