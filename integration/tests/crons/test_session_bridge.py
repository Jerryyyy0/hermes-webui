"""Tests for cron session materialize and sidebar visibility."""

import datetime as dt
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
                session_id TEXT,
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
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m1", "cron_job1_1700000000", "user", "hello", 1700000000.0),
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


def test_delete_cron_session_source_prevents_rematerialize(cron_env, monkeypatch):
    """Deleting a materialized cron session must remove state.db so import cannot revive it."""
    pytest.importorskip("cron.jobs")
    sid = "cron_job1_1700000000"
    job = {"id": "job1", "name": "Nightly", "profile": ""}
    owner = "default"
    out_dir = cron_env["home"] / "cron" / "output" / "job1"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "run1.md"
    out_file.write_text("## Response\n\nHello from cron", encoding="utf-8")
    # Ensure mtime maps back to this session candidate.
    os.utime(out_file, (1700000002.0, 1700000002.0))

    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "hi"}]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import (
                delete_materialized_cron_session_source,
                materialize_cron_session,
            )

            materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
            )

    from api.models import Session

    assert Session.load(sid) is not None

    def _profile_home(name):
        return cron_env["home"]

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": owner, "path": str(cron_env["home"])}],
        ):
            result = delete_materialized_cron_session_source(sid, profile_hint=owner)
    assert result.get("deleted") is True
    assert not out_file.exists()

    state_dir = Path(os.environ["HERMES_WEBUI_STATE_DIR"])
    sidecar = state_dir / "sessions" / f"{sid}.json"
    sidecar.unlink(missing_ok=True)

    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        cur = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,))
        assert cur.fetchone() is None

    with patch("api.models.get_state_db_session_messages", return_value=[]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session

            rematerialized = materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
            )

    assert rematerialized is None
    assert Session.load(sid) is None


def test_delete_cron_session_removes_output_from_owner_and_state_from_execution(tmp_path, monkeypatch):
    """Cron output lives in owner profile store; state.db row lives in execution profile."""
    pytest.importorskip("cron.jobs")
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    owner_home = tmp_path / "owner"
    exec_home = tmp_path / "exec"
    owner_home.mkdir(parents=True)
    exec_home.mkdir(parents=True)

    sid = "cron_job1_20260530_160041"
    job_id = "job1"
    out_dir = owner_home / "cron" / "output" / job_id
    out_dir.mkdir(parents=True)
    older_file = out_dir / "run_131651.md"
    older_file.write_text("## Response\n\nOlder", encoding="utf-8")
    os.utime(older_file, (1780118211.0, 1780118211.0))
    out_file = out_dir / "run_160127.md"
    out_file.write_text("## Response\n\nHello", encoding="utf-8")
    os.utime(out_file, (1780128087.0, 1780128087.0))
    (owner_home / "cron").mkdir(parents=True, exist_ok=True)
    (owner_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": job_id, "name": "Nightly", "profile": "exec"}]}),
        encoding="utf-8",
    )

    exec_db = exec_home / "state.db"
    with closing(sqlite3.connect(str(exec_db))) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, started_at REAL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (sid, "Cron run", "cron", 1780128042.448421),
        )
        conn.commit()

    def _profile_home(name):
        if name == "exec":
            return exec_home
        return owner_home

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "integration.crons.session_bridge.resolve_owner_profile_for_job",
            return_value="default",
        ):
            with patch(
                "api.profiles.list_profiles_api",
                return_value=[
                    {"name": "default", "path": str(owner_home)},
                    {"name": "exec", "path": str(exec_home)},
                ],
            ):
                from integration.crons.session_bridge import delete_materialized_cron_session_source

                result = delete_materialized_cron_session_source(sid, profile_hint="default")

    assert result.get("deleted") is True
    assert not out_file.exists()
    assert older_file.exists()
    with closing(sqlite3.connect(str(exec_db))) as conn:
        assert conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone() is None


def test_delete_orphan_cron_output_when_state_row_already_gone(cron_env, monkeypatch):
    """When the target cron session row is gone, still delete the nearest orphan .md."""
    pytest.importorskip("cron.jobs")
    sid = "cron_job1_20260530_153152"
    target_ts = dt.datetime.strptime("20260530_153152", "%Y%m%d_%H%M%S").timestamp()
    owner = "default"
    out_dir = cron_env["home"] / "cron" / "output" / "job1"
    out_dir.mkdir(parents=True, exist_ok=True)

    orphan_file = out_dir / "2026-05-30_15-26-51.md"
    orphan_file.write_text("## Response\n\nOrphan run", encoding="utf-8")
    os.utime(orphan_file, (target_ts - 300.0, target_ts - 300.0))

    unrelated_file = out_dir / "2026-05-30_14-00-00.md"
    unrelated_file.write_text("## Response\n\nOlder run", encoding="utf-8")
    os.utime(unrelated_file, (target_ts - 7200.0, target_ts - 7200.0))

    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        assert conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone() is None

    def _profile_home(name):
        return cron_env["home"]

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": owner, "path": str(cron_env["home"])}],
        ):
            from integration.crons.session_bridge import delete_materialized_cron_session_source

            result = delete_materialized_cron_session_source(sid, profile_hint=owner)

    assert result.get("deleted") is True
    assert not orphan_file.exists()
    assert unrelated_file.exists()


def test_delete_orphan_cron_output_skips_when_nearest_file_beyond_threshold(cron_env, monkeypatch):
    """Do not delete unrelated history when the nearest .md is too far from the session timestamp."""
    pytest.importorskip("cron.jobs")
    sid = "cron_job1_20260530_153152"
    target_ts = dt.datetime.strptime("20260530_153152", "%Y%m%d_%H%M%S").timestamp()
    owner = "default"
    out_dir = cron_env["home"] / "cron" / "output" / "job1"
    out_dir.mkdir(parents=True, exist_ok=True)

    distant_file = out_dir / "2026-05-30_14-00-00.md"
    distant_file.write_text("## Response\n\nToo far", encoding="utf-8")
    os.utime(distant_file, (target_ts - 7200.0, target_ts - 7200.0))

    def _profile_home(name):
        return cron_env["home"]

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": owner, "path": str(cron_env["home"])}],
        ):
            from integration.crons.session_bridge import delete_materialized_cron_session_source

            result = delete_materialized_cron_session_source(sid, profile_hint=owner)

    assert result.get("deleted") is False
    assert distant_file.exists()
    assert result.get("deleted_output_files") == []


def test_delete_cron_job_history_removes_all_job_runs_and_preserves_other_jobs(cron_env, monkeypatch):
    """Deleting a cron job should not leave history sessions or output artifacts behind."""
    pytest.importorskip("cron.jobs")
    owner = "default"
    job_id = "job1"
    other_job_id = "job2"

    job1_out = cron_env["home"] / "cron" / "output" / job_id
    job1_out.mkdir(parents=True, exist_ok=True)
    first_output = job1_out / "run1.md"
    second_output = job1_out / "run2.md"
    first_output.write_text("## Response\n\nfirst", encoding="utf-8")
    second_output.write_text("## Response\n\nsecond", encoding="utf-8")

    job2_out = cron_env["home"] / "cron" / "output" / other_job_id
    job2_out.mkdir(parents=True, exist_ok=True)
    other_output = job2_out / "other.md"
    other_output.write_text("## Response\n\nother", encoding="utf-8")

    job1_sid_1 = "cron_job1_1700000000"
    job1_sid_2 = "cron_job1_1700000100"
    job2_sid = "cron_job2_1700000200"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (job1_sid_2, "Cron run 2", "cron", 1700000100.0),
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (job2_sid, "Other cron run", "cron", 1700000200.0),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m2", job1_sid_2, "assistant", "second", 1700000101.0),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m3", job2_sid, "assistant", "other", 1700000201.0),
        )
        conn.commit()

    state_dir = Path(os.environ["HERMES_WEBUI_STATE_DIR"])
    sessions_dir = state_dir / "sessions"
    for sid in (job1_sid_1, job1_sid_2, job2_sid):
        (sessions_dir / f"{sid}.json").write_text(
            json.dumps({"session_id": sid, "source_tag": "cron"}),
            encoding="utf-8",
        )

    def _profile_home(name):
        return cron_env["home"]

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": owner, "path": str(cron_env["home"])}],
        ):
            from integration.crons.session_bridge import delete_cron_job_history

            result = delete_cron_job_history(
                job_id,
                owner_profile=owner,
                job={"id": job_id, "profile": ""},
            )

    assert result.get("deleted") is True
    assert not job1_out.exists()
    assert other_output.exists()
    assert not (sessions_dir / f"{job1_sid_1}.json").exists()
    assert not (sessions_dir / f"{job1_sid_2}.json").exists()
    assert (sessions_dir / f"{job2_sid}.json").exists()
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        assert conn.execute("SELECT id FROM sessions WHERE id LIKE 'cron_job1_%'").fetchall() == []
        assert conn.execute("SELECT id FROM sessions WHERE id = ?", (job2_sid,)).fetchone() is not None
        assert conn.execute("SELECT id FROM messages WHERE session_id = ?", (job1_sid_2,)).fetchone() is None
        assert conn.execute("SELECT id FROM messages WHERE session_id = ?", (job2_sid,)).fetchone() is not None


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
