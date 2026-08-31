"""Tests for cron session materialize and sidebar visibility."""

import datetime as dt
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    home = tmp_path / "profile_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_DEFAULT_WORKSPACE", str(workspace))

    state_dir = Path(os.environ["HERMES_WEBUI_STATE_DIR"])
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    import api.config as webui_config
    import api.models as models
    import api.workspace as workspace_api

    monkeypatch.setattr(webui_config, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(webui_config, "STATE_DIR", state_dir)
    monkeypatch.setattr(webui_config, "DEFAULT_WORKSPACE", workspace)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(workspace_api, "_BOOT_DEFAULT_WORKSPACE", workspace)

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

    return {"home": home, "db": db, "workspace": workspace}


def test_cron_sessions_never_visible_in_sidebar(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from integration.crons.session_bridge import cron_sessions_visible_in_sidebar

    assert cron_sessions_visible_in_sidebar({"source_tag": "cron", "is_cli_session": False}) is False
    assert cron_sessions_visible_in_sidebar({"source_tag": "cron", "is_cli_session": True}) is False


def test_materialized_cron_session_ids_for_runs_maps_existing_sidecar(cron_env):
    from api.config import SESSION_DIR
    from integration.crons.session_bridge import materialized_cron_session_ids_for_runs

    sid = "cron_job9_20260609_190107"
    (Path(SESSION_DIR) / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "source_tag": "cron"}),
        encoding="utf-8",
    )

    runs = [
        {
            "filename": "2026-06-09_19-01-15.md",
            "modified": dt.datetime(2026, 6, 9, 19, 1, 15).timestamp(),
        }
    ]

    assert materialized_cron_session_ids_for_runs("job9", runs) == {
        "2026-06-09_19-01-15.md": sid
    }


def test_hide_sidebar_cron_sessions_always_hidden(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from api.models import _hide_from_default_sidebar

    assert _hide_from_default_sidebar({"session_id": "cron_x", "source_tag": "cron", "is_cli_session": False}) is True
    assert _hide_from_default_sidebar({"session_id": "cron_x", "source_tag": "cron", "is_cli_session": True}) is True


def test_resolve_cron_execution_ended_at_backfills_from_execution_profile(cron_env, monkeypatch):
    from integration.crons import session_bridge

    sid = "cron_job1_1700000000"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (1700000250.0, sid))
        conn.commit()
    monkeypatch.setattr(session_bridge, "_profile_home_for_name", lambda profile: cron_env["home"])

    session = SimpleNamespace(
        session_id=sid,
        source_tag="cron",
        profile="owner",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
    )

    assert session_bridge.resolve_cron_execution_ended_at(session) == 1700000250.0


def test_resolve_cron_execution_ended_at_keeps_existing_sidecar_boundary(cron_env, monkeypatch):
    from integration.crons import session_bridge

    monkeypatch.setattr(
        session_bridge,
        "_profile_home_for_name",
        lambda _profile: (_ for _ in ()).throw(AssertionError("state.db must not be read")),
    )
    session = SimpleNamespace(
        session_id="cron_job1_1700000000",
        source_tag="cron",
        profile="owner",
        cron_execution_profile="execution",
        cron_execution_ended_at=1700000200.0,
    )

    assert session_bridge.resolve_cron_execution_ended_at(session) == 1700000200.0


def test_resolve_cron_execution_ended_at_rejects_legacy_state_db_without_boundary(cron_env, monkeypatch):
    from integration.crons import session_bridge

    monkeypatch.setattr(session_bridge, "_profile_home_for_name", lambda profile: cron_env["home"])
    session = SimpleNamespace(
        session_id="cron_job1_1700000000",
        source_tag="cron",
        profile="owner",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
    )

    assert session_bridge.resolve_cron_execution_ended_at(session) is None


def test_filter_cron_sessions_from_sidebar_rows(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from integration.crons.session_bridge import filter_cron_sessions_from_sidebar_rows

    rows = [
        {"session_id": "cron_job_20260623_111836", "source_tag": "cron", "default_hidden": True},
        {"session_id": "94b8df15f0e2", "source_tag": "webui"},
        {"session_id": "cli-1", "source_tag": "cli"},
    ]
    filtered = filter_cron_sessions_from_sidebar_rows(rows)
    assert [row["session_id"] for row in filtered] == ["94b8df15f0e2", "cli-1"]


def test_apply_integration_sidebar_session_filters_in_routes(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    from api.routes import _apply_integration_sidebar_session_filters

    rows = [
        {"session_id": "cron_job_20260623_111836", "source_tag": "cron"},
        {"session_id": "webui-1", "source_tag": "webui"},
    ]
    assert [row["session_id"] for row in _apply_integration_sidebar_session_filters(rows)] == ["webui-1"]


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
    assert Session.load(sid).messages[0]["_turn_key"] == "turn:1"


def test_materialize_persists_execution_boundary_from_state_db(cron_env, monkeypatch):
    pytest.importorskip("cron.jobs")
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (1700000250.0, "cron_job1_1700000000"),
        )
        conn.commit()

    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "hi"}]):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": "default", "path": str(cron_env["home"])}],
        ):
            from integration.crons.session_bridge import materialize_cron_session

            sid = materialize_cron_session(
                {"id": "job1", "name": "Nightly", "profile": ""},
                owner_profile="default",
                execution_home=cron_env["home"],
            )

    from api.models import Session

    assert Session.load(sid).cron_execution_ended_at == 1700000250.0


def test_materialize_collapses_compaction_replayed_execution_prompt(cron_env, monkeypatch):
    pytest.importorskip("cron.jobs")
    sid = "cron_job1_1700000000"
    prompt = "generate the scheduled briefing"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN ended_at REAL")
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (1700000250.0, sid),
        )
        conn.commit()
    messages = [
        {"role": "user", "content": prompt, "timestamp": 1700000001.0},
        {
            "role": "assistant",
            "content": "compressed context",
            "timestamp": 1700000100.0,
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "compaction_summary",
        },
        {"role": "assistant", "content": "briefing ready", "timestamp": 1700000200.0},
        {"role": "user", "content": prompt, "timestamp": 1700000201.0},
    ]
    monkeypatch.setattr("api.models.get_state_db_session_messages", lambda *_args, **_kwargs: messages)
    with patch(
        "api.profiles.list_profiles_api",
        return_value=[{"name": "default", "path": str(cron_env["home"])}],
    ):
        from integration.crons.session_bridge import materialize_cron_session

        assert materialize_cron_session(
            {"id": "job1", "name": "Nightly", "profile": ""},
            owner_profile="default",
            execution_home=cron_env["home"],
        ) == sid

    from api.models import Session

    materialized = Session.load(sid)
    users = [message for message in materialized.messages if message.get("role") == "user"]
    assert [message["content"] for message in users] == [prompt]
    assert [message.get("_turn_key") for message in users] == ["turn:1"]


def test_current_v1_materialize_uses_selected_state_db_cwd_without_transient_handoff(cron_env, monkeypatch):
    workspace = cron_env["workspace"] / "sessions" / "cron" / "default" / "cron_job1_1700000000"
    workspace.mkdir(parents=True)
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        conn.execute(
            "UPDATE sessions SET cwd = ? WHERE id = ?",
            (str(workspace), "cron_job1_1700000000"),
        )
        conn.commit()

    job = {
        "id": "job1",
        "name": "Nightly",
        "profile": "default",
        "workspace_policy": {
            "version": 1,
            "strategy": "managed",
            "base_workspace": str(cron_env["workspace"]),
        },
    }
    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "hi"}]):
        from integration.crons.session_bridge import materialize_cron_session

        sid = materialize_cron_session(
            job,
            owner_profile="default",
            execution_home=cron_env["home"],
            session_id="cron_job1_1700000000",
        )

    from api.models import Session

    session = Session.load(sid)
    assert session.workspace == str(workspace)
    assert session.workspace_mode == "managed"
    assert session.workspace_state == "ready"


def test_unverified_v1_sidecar_repairs_before_user_followup(cron_env):
    from api.models import Session
    from integration.crons.hooks import _MAX_ITERATION_SUMMARY_REQUEST
    from integration.crons.session_bridge import materialize_cron_session

    sid = "cron_job1_1700000000"
    execution_workspace = cron_env["workspace"] / "sessions" / "cron" / "default" / sid
    execution_workspace.mkdir(parents=True)
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(execution_workspace), sid))
        conn.commit()

    Session(
        session_id=sid,
        title="Nightly",
        profile="default",
        source_tag="cron",
        workspace=str(cron_env["workspace"]),
        workspace_mode="external",
        workspace_state="workspace_unverified",
        messages=[
            {"role": "user", "content": "run"},
            {"role": "assistant", "tool_calls": [{"id": "tool-1"}]},
            {"role": "tool", "content": "tool result"},
            {"role": "user", "content": _MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": "done"},
        ],
    ).save()

    materialize_cron_session(
        {
            "id": "job1",
            "name": "Nightly",
            "profile": "default",
            "workspace_policy": {
                "version": 1,
                "strategy": "managed",
                "base_workspace": str(cron_env["workspace"]),
            },
        },
        owner_profile="default",
        execution_home=cron_env["home"],
        session_id=sid,
    )

    repaired = Session.load(sid)
    assert repaired.workspace == str(execution_workspace)
    assert repaired.workspace_mode == "managed"
    assert repaired.workspace_state == "ready"


def test_unverified_v1_sidecar_with_user_followup_is_not_rebound(cron_env):
    from api.models import Session
    from integration.crons.session_bridge import materialize_cron_session

    sid = "cron_job1_1700000000"
    execution_workspace = cron_env["workspace"] / "sessions" / "cron" / "default" / sid
    execution_workspace.mkdir(parents=True)
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(execution_workspace), sid))
        conn.commit()

    fallback_workspace = str(cron_env["workspace"])
    Session(
        session_id=sid,
        title="Nightly",
        profile="default",
        source_tag="cron",
        workspace=fallback_workspace,
        workspace_mode="external",
        workspace_state="workspace_unverified",
        messages=[
            {"role": "user", "content": "run"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "continue"},
        ],
    ).save()

    materialize_cron_session(
        {
            "id": "job1",
            "name": "Nightly",
            "profile": "default",
            "workspace_policy": {
                "version": 1,
                "strategy": "managed",
                "base_workspace": str(cron_env["workspace"]),
            },
        },
        owner_profile="default",
        execution_home=cron_env["home"],
        session_id=sid,
    )

    preserved = Session.load(sid)
    assert preserved.workspace == fallback_workspace
    assert preserved.workspace_state == "workspace_unverified"


def test_legacy_empty_cwd_uses_profile_shared_continuation_workspace(cron_env, monkeypatch):
    """Legacy records without Agent cwd remain usable in the approved shared root."""
    from integration.crons.session_bridge import materialize_cron_session

    monkeypatch.setattr(
        "integration.crons.workspace_policy.approved_default_workspace",
        lambda _profile_home: cron_env["workspace"],
    )
    with patch(
        "api.models.get_state_db_session_messages",
        return_value=[{"role": "user", "content": "hi"}],
    ):
        sid = materialize_cron_session(
            {"id": "job1", "name": "Legacy nightly", "profile": ""},
            owner_profile="default",
            execution_home=cron_env["home"],
        )

    from api.models import Session
    from api.workspace import resolve_session_workspace

    session = Session.load(sid)
    assert session.workspace == str(cron_env["workspace"])
    assert session.workspace_mode == "external"
    assert session.workspace_state == "legacy_shared"
    assert resolve_session_workspace(session) == cron_env["workspace"].resolve()


def test_legacy_history_materializers_use_shared_continuation_workspace(cron_env, monkeypatch):
    from integration.crons.session_bridge import (
        materialize_cron_session_run,
        materialize_cron_sessions_for_runs,
    )

    monkeypatch.setattr(
        "integration.crons.workspace_policy.approved_default_workspace",
        lambda _profile_home: cron_env["workspace"],
    )
    monkeypatch.setattr(
        "integration.crons.session_bridge._profile_home_for_name",
        lambda _profile: cron_env["home"],
    )
    job = {"id": "job1", "name": "Legacy nightly", "profile": ""}
    run = {
        "session_id": "cron_job1_1700000000",
        "title": "Legacy nightly",
        "started_at": 1700000000.0,
        "ended_at": 1700000001.0,
    }

    assert materialize_cron_session_run(job, owner_profile="default", run=run) == run["session_id"]
    session_ids = materialize_cron_sessions_for_runs(
        job,
        owner_profile="default",
        execution_home=cron_env["home"],
        runs=[{"filename": "legacy.md", "run_mtime": 1700000000.0}],
    )

    from api.models import Session

    assert session_ids == {"legacy.md": run["session_id"]}
    assert Session.load(run["session_id"]).workspace_state == "legacy_shared"


def test_v1_history_run_uses_exact_state_db_workspace_record(cron_env, monkeypatch):
    from integration.crons import session_bridge

    sid = "cron_job1_1700000000"
    workspace = cron_env["workspace"] / "sessions" / "cron" / "default" / sid
    workspace.mkdir(parents=True)
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(workspace), sid))
        conn.commit()
    monkeypatch.setattr(session_bridge, "_profile_home_for_name", lambda _profile: cron_env["home"])

    result = session_bridge.materialize_cron_session_run(
        {
            "id": "job1",
            "name": "V1 nightly",
            "profile": "default",
            "workspace_policy": {
                "version": 1,
                "strategy": "managed",
                "base_workspace": str(cron_env["workspace"]),
            },
        },
        owner_profile="default",
        run={"session_id": sid, "title": "V1 nightly", "started_at": 1700000000.0},
    )

    from api.models import Session

    session = Session.load(result)
    assert session.workspace == str(workspace)
    assert session.workspace_mode == "managed"
    assert session.workspace_state == "ready"


def test_v1_history_batch_uses_exact_state_db_workspace_records(cron_env, monkeypatch):
    from integration.crons import session_bridge

    sid = "cron_job1_1700000000"
    workspace = cron_env["workspace"] / "sessions" / "cron" / "default" / sid
    workspace.mkdir(parents=True)
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(workspace), sid))
        conn.commit()

    session_ids = session_bridge.materialize_cron_sessions_for_runs(
        {
            "id": "job1",
            "name": "V1 nightly",
            "profile": "default",
            "workspace_policy": {
                "version": 1,
                "strategy": "managed",
                "base_workspace": str(cron_env["workspace"]),
            },
        },
        owner_profile="default",
        execution_home=cron_env["home"],
        runs=[{"filename": "v1.md", "run_mtime": 1700000000.0}],
    )

    from api.models import Session

    session = Session.load(session_ids["v1.md"])
    assert session.workspace == str(workspace)
    assert session.workspace_mode == "managed"
    assert session.workspace_state == "ready"


def test_legacy_same_root_sidecar_is_repaired_from_unverified(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import materialize_cron_session

    sid = "cron_job1_1700000000"
    Session(
        session_id=sid,
        title="Legacy nightly",
        profile="default",
        source_tag="cron",
        workspace=str(cron_env["workspace"]),
        workspace_mode="external",
        workspace_state="workspace_unverified",
        messages=[{"role": "user", "content": "hi"}],
    ).save()
    monkeypatch.setattr(
        "integration.crons.workspace_policy.approved_default_workspace",
        lambda _profile_home: cron_env["workspace"],
    )

    materialize_cron_session(
        {"id": "job1", "name": "Legacy nightly", "profile": ""},
        owner_profile="default",
        execution_home=cron_env["home"],
    )

    repaired = Session.load(sid)
    assert repaired.workspace == str(cron_env["workspace"])
    assert repaired.workspace_state == "legacy_shared"


def test_v1_missing_current_workspace_handoff_remains_unverified(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    sid = materialize_cron_session(
        {
            "id": "job1",
            "name": "V1 nightly",
            "profile": "default",
            "_cron_session_id": "cron_job1_1700000000",
            "workspace_policy": {
                "version": 1,
                "strategy": "managed",
                "base_workspace": str(cron_env["workspace"]),
            },
        },
        owner_profile="default",
        execution_home=cron_env["home"],
        session_id="cron_job1_1700000000",
    )

    from api.models import Session

    assert Session.load(sid).workspace_state == "workspace_unverified"


def test_unverified_cron_workspace_uses_shared_default_for_continuation(cron_env):
    from api.workspace import resolve_session_workspace

    session = SimpleNamespace(
        workspace=str(cron_env["home"]),
        workspace_mode="external",
        workspace_state="workspace_unverified",
        source_tag="cron",
    )
    assert resolve_session_workspace(session) == cron_env["workspace"].resolve()


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


def test_materialize_selects_exact_session_id_not_latest_run(cron_env):
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
            session_id="cron_job1_1700000000",
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


def test_materialize_existing_empty_sidecar_restores_fallback_user_anchor(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import materialize_cron_session

    sid = "cron_job1_1700000000"
    sidecar = Session(
        session_id=sid,
        title="Nightly",
        profile="default",
        messages=[],
    )
    sidecar.source_tag = "cron"
    sidecar.cron_execution_profile = str(cron_env["home"])
    sidecar.save()

    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    with patch("api.models.get_state_db_session_messages", return_value=[]):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": "default", "path": str(cron_env["home"])}],
        ):
            materialize_cron_session(
                job,
                owner_profile="default",
                execution_home=cron_env["home"],
                fallback_output="## Response\n\nHello from cron",
                run_mtime=1700000001.0,
            )

    full = Session.load(sid)
    assert full is not None
    assert [message["role"] for message in full.messages] == ["user", "assistant"]
    assert full.messages[0]["content"] == "run nightly"
    assert full.messages[1]["content"] == "Hello from cron"


def test_materialize_no_agent_output_creates_stable_session(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        fallback_output="## Response\n\nscript completed",
        fallback_filename="2026-07-31_12-00-05.md",
    )

    assert sid == "cron_script1_20260731_120005"
    from api.models import Session

    session = Session.load(sid)
    assert session is not None
    assert [message["content"] for message in session.messages] == [
        "定时脚本任务「Watchdog」的本次运行结果如下。",
        "script completed",
    ]


def test_materialize_no_agent_binds_profile_model_provider(cron_env, monkeypatch):
    from integration.crons import session_bridge

    monkeypatch.setattr(
        session_bridge,
        "read_profile_default_binding",
        lambda _home: ("deepseek-v4", "deepseek"),
        raising=False,
    )
    sid = session_bridge.materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        fallback_output="## Response\n\nscript completed",
        fallback_filename="2026-07-31_12-00-05.md",
    )

    from api.models import Session

    session = Session.load(sid)
    assert session.model == "deepseek-v4"
    assert session.model_provider == "deepseek"
    assert session.model != "unknown"


def test_materialize_no_agent_failure_persists_cron_error(cron_env):
    from integration.crons.session_bridge import (
        list_cron_job_runs_from_state_db,
        materialize_cron_session,
    )

    output = (
        "# Cron Job: Watchdog\n\n"
        "**Mode:** no_agent (script)\n"
        "**Status:** script failed\n\n"
        "Script not found: /tmp/watchdog.sh"
    )
    cron_env["db"].unlink()
    from hermes_state import SessionDB

    store = SessionDB(db_path=cron_env["db"])
    store.close()
    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        run_mtime=1785480005.0,
        fallback_output=output,
        fallback_filename="2026-07-31_12-00-05.md",
        execution_end_reason="cron_error",
    )

    assert sid == "cron_script1_20260731_120005"
    runs = list_cron_job_runs_from_state_db(cron_env["home"], "script1")
    assert [(run["session_id"], run["end_reason"]) for run in runs] == [
        (sid, "cron_error")
    ]
    from api.models import Session

    session = Session.load(sid)
    assert session.last_error_at is not None
    error_messages = [message for message in session.messages if message.get("_error")]
    assert len(error_messages) == 1
    assert error_messages[0]["_error_type"] == "cron_script_error"
    assert error_messages[0]["content"].startswith("**脚本执行失败:")
    assert error_messages[0]["provider_details"] == "Script not found: /tmp/watchdog.sh"
    assert error_messages[0]["provider_details_label"] == "脚本错误详情"


def test_materialize_no_agent_failure_replaces_legacy_provider_error(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    output = (
        "# Cron Job: Watchdog\n\n"
        "**Mode:** no_agent (script)\n"
        "**Status:** script failed\n\n"
        "Script not found: /tmp/watchdog.sh"
    )
    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        run_mtime=1785480005.0,
        fallback_output=output,
        fallback_filename="2026-07-31_12-00-05.md",
        execution_end_reason="cron_error",
    )

    from api.models import Session

    session = Session.load(sid)
    session.messages.append(
        {
            "role": "assistant",
            "content": "**未找到模型:** 当前 Provider 找不到所选模型。",
            "timestamp": 1785480005.0,
            "_error": True,
            "_error_type": "model_not_found",
            "provider_details": output,
            "provider_details_label": "技术详情",
        }
    )
    session.save()

    assert materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        run_mtime=1785480005.0,
        fallback_output=output,
        fallback_filename="2026-07-31_12-00-05.md",
        execution_end_reason="cron_error",
    ) == sid

    repaired = Session.load(sid)
    error_messages = [message for message in repaired.messages if message.get("_error")]
    assert len(error_messages) == 1
    assert error_messages[0]["_error_type"] == "cron_script_error"
    assert error_messages[0]["provider_details"] == "Script not found: /tmp/watchdog.sh"


def test_agent_cron_failure_keeps_provider_error_classification():
    from integration.crons.session_bridge import _build_cron_error_message

    message = _build_cron_error_message(
        {"id": "agent1", "name": "Agent task"},
        "## Error\n\nmodel not found (FAILED)",
        end_reason="cron_error",
        timestamp=1785480005.0,
    )

    assert message["_error_type"] == "model_not_found"
    assert message["provider_details_label"] == "技术详情"


def test_materialize_no_agent_output_uses_sidecar_when_state_db_is_unavailable(cron_env):
    cron_env["db"].unlink()
    from integration.crons.session_bridge import materialize_cron_session

    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        fallback_output="## Response\n\nscript completed",
        fallback_filename="2026-07-31_12-00-05.md",
    )

    assert sid == "cron_script1_20260731_120005"
    from api.models import Session

    session = Session.load(sid)
    assert session is not None
    assert session.cron_execution_ended_at is None


def test_materialize_no_agent_output_uses_sidecar_when_state_db_read_fails(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    with patch(
        "integration.crons.session_bridge.sqlite3.connect",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        sid = materialize_cron_session(
            {"id": "script1", "name": "Watchdog", "no_agent": True},
            owner_profile="default",
            execution_home=cron_env["home"],
            fallback_output="## Response\n\nscript completed",
            fallback_filename="2026-07-31_12-00-05.md",
        )

    assert sid == "cron_script1_20260731_120005"


def test_backfill_no_agent_output_persists_history_record(cron_env, caplog):
    from integration.crons.session_bridge import (
        backfill_cron_output_runs_to_state_db,
        list_cron_job_runs_from_state_db,
    )

    cron_env["db"].unlink()
    from hermes_state import SessionDB

    store = SessionDB(db_path=cron_env["db"])
    store.close()
    caplog.set_level(logging.INFO)
    result = backfill_cron_output_runs_to_state_db(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        execution_home=cron_env["home"],
        artifacts=[
            {
                "filename": "2026-07-31_12-00-05.md",
                "modified": 1785480005.0,
                "fallback_output": "## Response\n\nscript completed",
            },
            {
                "filename": "2026-07-31_12-01-05.md",
                "modified": 1785480065.0,
                "fallback_output": "## Response\n\nscript completed again",
            }
        ],
        database_runs=[],
    )

    assert result == {"imported": 2, "skipped": 0}
    assert [run["session_id"] for run in list_cron_job_runs_from_state_db(cron_env["home"], "script1")] == [
        "cron_script1_20260731_120105",
        "cron_script1_20260731_120005"
    ]
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        titles = [row[0] for row in conn.execute("SELECT title FROM sessions ORDER BY id")]
    assert titles == [
        "Watchdog · 2026-07-31 12:00:05",
        "Watchdog · 2026-07-31 12:01:05",
    ]
    assert "cron output backfilled job_id=script1" in caplog.text
    assert "script completed" not in caplog.text


def test_execution_results_match_empty_no_agent_artifact_as_failure(cron_env):
    from integration.crons.session_bridge import match_cron_artifacts_to_execution_results

    artifacts = [
        {
            "filename": "2026-07-31_11-09-18.md",
            "modified": 1785467358.2,
            "size": 0,
            "fallback_output": "",
        }
    ]
    match_cron_artifacts_to_execution_results(
        artifacts,
        [
            {
                "execution_id": "failed-run",
                "status": "failed",
                "started_at": 1785467357.0,
                "ended_at": 1785467358.0,
                "claimed_at": 1785467356.0,
                "end_reason": "cron_error",
                "error_detail": "no_agent=True but no script is set for this job",
            }
        ],
    )

    assert artifacts[0]["execution_end_reason"] == "cron_error"
    assert artifacts[0]["execution_error_detail"] == "no_agent=True but no script is set for this job"


def test_execution_results_are_one_to_one_for_neighboring_artifacts(cron_env):
    from integration.crons.session_bridge import match_cron_artifacts_to_execution_results

    artifacts = [
        {"filename": "first.md", "modified": 100.0},
        {"filename": "second.md", "modified": 106.0},
    ]
    executions = [
        {
            "execution_id": "first",
            "status": "completed",
            "ended_at": 101.0,
            "started_at": 100.0,
            "claimed_at": 99.0,
            "end_reason": "cron_complete",
            "error_detail": None,
        },
        {
            "execution_id": "second",
            "status": "failed",
            "ended_at": 105.0,
            "started_at": 104.0,
            "claimed_at": 103.0,
            "end_reason": "cron_error",
            "error_detail": "script exited 1",
        },
    ]

    match_cron_artifacts_to_execution_results(artifacts, executions)

    assert [artifact["execution_end_reason"] for artifact in artifacts] == [
        "cron_complete",
        "cron_error",
    ]


def test_list_cron_execution_results_reads_terminal_statuses(cron_env):
    from integration.crons.session_bridge import list_cron_job_execution_results

    execution_db = cron_env["home"] / "cron" / "executions.db"
    execution_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(execution_db))) as conn:
        conn.execute(
            """
            CREATE TABLE executions (
                id TEXT PRIMARY KEY, job_id TEXT, status TEXT, claimed_at TEXT,
                started_at TEXT, finished_at TEXT, error TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "failed-run",
                "script1",
                "failed",
                "2026-07-31T11:09:17+08:00",
                "2026-07-31T11:09:17+08:00",
                "2026-07-31T11:09:18+08:00",
                "no_agent=True but no script is set for this job",
            ),
        )
        conn.commit()

    assert list_cron_job_execution_results(cron_env["home"], "script1") == [
        {
            "execution_id": "failed-run",
            "status": "failed",
            "started_at": 1785467357.0,
            "ended_at": 1785467358.0,
            "claimed_at": 1785467357.0,
            "end_reason": "cron_error",
            "error_detail": "no_agent=True but no script is set for this job",
        }
    ]


def test_empty_no_agent_artifact_without_execution_is_backfilled_as_unknown(cron_env):
    from integration.crons.session_bridge import (
        backfill_cron_output_runs_to_state_db,
        list_cron_job_runs_from_state_db,
    )

    cron_env["db"].unlink()
    from hermes_state import SessionDB

    store = SessionDB(db_path=cron_env["db"])
    store.close()
    result = backfill_cron_output_runs_to_state_db(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        execution_home=cron_env["home"],
        artifacts=[
            {
                "filename": "2026-07-31_11-09-18.md",
                "modified": 1785476958.0,
                "size": 0,
                "fallback_output": "",
            }
        ],
        database_runs=[],
    )

    assert result == {"imported": 1, "skipped": 0}
    runs = list_cron_job_runs_from_state_db(cron_env["home"], "script1")
    assert [(run["session_id"], run["end_reason"]) for run in runs] == [
        ("cron_script1_20260731_110918", None)
    ]


def test_job_last_run_error_matches_only_its_empty_no_agent_artifact(cron_env):
    from integration.crons.session_bridge import match_cron_artifacts_to_job_last_run_result

    artifacts = [
        {
            "filename": "2026-07-31_12-18-09.md",
            "modified": 1785471489.6611462,
            "size": 0,
            "fallback_output": "",
        },
        {
            "filename": "2026-07-31_12-21-10.md",
            "modified": 1785471670.0335526,
            "size": 0,
            "fallback_output": "",
        },
    ]

    match_cron_artifacts_to_job_last_run_result(
        artifacts,
        {
            "id": "script1",
            "no_agent": True,
            "last_run_at": "2026-07-31T12:21:10.070677+08:00",
            "last_status": "error",
            "last_error": "no_agent=True but no script is set for this job",
        },
    )

    assert "execution_end_reason" not in artifacts[0]
    assert artifacts[1]["execution_status"] == "failed"
    assert artifacts[1]["execution_end_reason"] == "cron_error"
    assert artifacts[1]["execution_error_detail"] == "no_agent=True but no script is set for this job"


def test_empty_no_agent_failure_is_backfilled_with_execution_error(cron_env):
    from integration.crons.session_bridge import (
        backfill_cron_output_runs_to_state_db,
        list_cron_job_runs_from_state_db,
    )

    cron_env["db"].unlink()
    from hermes_state import SessionDB

    store = SessionDB(db_path=cron_env["db"])
    store.close()
    error = "no_agent=True but no script is set for this job"
    result = backfill_cron_output_runs_to_state_db(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        execution_home=cron_env["home"],
        artifacts=[
            {
                "filename": "2026-07-31_11-09-18.md",
                "modified": 1785476958.0,
                "size": 0,
                "fallback_output": "",
                "execution_end_reason": "cron_error",
                "execution_error_detail": error,
            }
        ],
        database_runs=[],
    )

    assert result == {"imported": 1, "skipped": 0}
    runs = list_cron_job_runs_from_state_db(cron_env["home"], "script1")
    assert [(run["session_id"], run["end_reason"]) for run in runs] == [
        ("cron_script1_20260731_110918", "cron_error")
    ]


def test_empty_no_agent_failure_materializes_script_error_sidecar(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    error = "no_agent=True but no script is set for this job"
    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        run_mtime=1785476958.0,
        fallback_output="",
        fallback_filename="2026-07-31_11-09-18.md",
        execution_end_reason="cron_error",
        execution_error_detail=error,
        execution_ended_at=1785476958.0,
    )

    from api.models import Session

    session = Session.load(sid)
    assert session is not None
    assert session.cron_execution_ended_at == 1785476958.0
    assert "脚本执行失败" in session.messages[1]["content"]
    assert session.messages[-1]["_error_type"] == "cron_script_error"
    assert session.messages[-1]["provider_details"] == error


def test_empty_no_agent_unknown_materializes_sidecar(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    cron_env["db"].unlink()
    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        run_mtime=1785476958.0,
        fallback_output="",
        fallback_filename="2026-07-31_11-09-18.md",
    )

    from api.models import Session

    session = Session.load(sid)
    assert sid == "cron_script1_20260731_110918"
    assert session is not None
    assert session.last_error_at is None
    assert session.messages[1]["content"] == "脚本任务未产生输出；本次运行状态尚未验证。"


def test_no_agent_runtime_failure_materializes_without_output_artifact(cron_env):
    from integration.crons.session_bridge import materialize_cron_session

    sid = materialize_cron_session(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        owner_profile="default",
        execution_home=cron_env["home"],
        session_id="cron_script1_20260731_120105",
        execution_end_reason="cron_error",
        execution_error_detail="no_agent=True but no script is set for this job",
    )

    from api.models import Session

    session = Session.load(sid)
    assert sid == "cron_script1_20260731_120105"
    assert session is not None
    assert session.messages[-1]["_error_type"] == "cron_script_error"


def test_failed_execution_corrects_existing_no_agent_synthetic_record(cron_env, caplog):
    from integration.crons.session_bridge import (
        backfill_cron_output_runs_to_state_db,
        reconcile_no_agent_cron_output_records,
    )

    cron_env["db"].unlink()
    from hermes_state import SessionDB

    store = SessionDB(db_path=cron_env["db"])
    store.close()
    job = {"id": "script1", "name": "Watchdog", "no_agent": True}
    artifact = {
        "filename": "2026-07-31_11-09-18.md",
        "modified": 1785476958.0,
        "fallback_output": "script completed",
    }
    assert backfill_cron_output_runs_to_state_db(
        job,
        execution_home=cron_env["home"],
        artifacts=[artifact],
        database_runs=[],
    )["imported"] == 1

    corrected_artifact = {
        **artifact,
        "execution_end_reason": "cron_error",
        "execution_error_detail": "no_agent=True but no script is set for this job",
    }
    database_runs = [{"session_id": "cron_script1_20260731_110918", "end_reason": "cron_complete"}]
    caplog.set_level(logging.INFO)
    reconcile_no_agent_cron_output_records(
        cron_env["home"],
        job,
        [corrected_artifact],
        database_runs,
    )

    assert database_runs[0]["end_reason"] == "cron_error"
    assert "cron output failure reconciled job_id=script1" in caplog.text
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        assert conn.execute(
            "SELECT end_reason FROM sessions WHERE id = ?",
            ("cron_script1_20260731_110918",),
        ).fetchone()[0] == "cron_error"


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

    from api import models

    with patch("integration.crons.session_bridge._profile_home_for_name", _profile_home):
        with patch(
            "api.profiles.list_profiles_api",
            return_value=[{"name": owner, "path": str(cron_env["home"])}],
        ):
            with patch(
                "api.models._delete_state_db_session_rows_many",
                wraps=models._delete_state_db_session_rows_many,
            ) as batch_delete:
                from integration.crons.session_bridge import delete_cron_job_history

                result = delete_cron_job_history(
                    job_id,
                    owner_profile=owner,
                    job={"id": job_id, "profile": ""},
                )

    assert result.get("deleted") is True
    batch_delete.assert_called_once()
    assert set(batch_delete.call_args.args[1]) == {job1_sid_1, job1_sid_2}
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


def test_delete_cron_job_history_preserves_records_when_workspace_cleanup_fails(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import delete_cron_job_history

    sid = "cron_job1_20260819_010101_deadbeef"
    session = Session(
        session_id=sid,
        title="Cron run",
        profile="default",
        source_tag="cron",
        workspace=str(cron_env["home"] / "workspace"),
        workspace_mode="managed",
        workspace_state="ready",
        messages=[{"role": "assistant", "content": "done"}],
    )
    session.save()
    with patch(
        "integration.crons.session_bridge._cron_session_ids_by_profile",
        return_value={"default": {sid}},
    ), patch(
        "integration.crons.session_bridge._cron_state_db_profiles_for_job_delete",
        return_value=["default"],
    ), patch(
        "integration.crons.session_bridge._cron_owner_profiles_for_job_delete",
        return_value=["default"],
    ), patch(
        "integration.crons.session_bridge._profile_home_for_name",
        return_value=cron_env["home"],
    ), patch(
        "integration.crons.worktree.cleanup_execution_workspace",
        return_value={"ok": False, "deleted": False, "reason": "busy"},
    ):
        result = delete_cron_job_history("job1", owner_profile="default", job={"id": "job1"})

    assert result["ok"] is False
    assert result["reason"] == "workspace_cleanup_failed"
    restored = Session.load(sid)
    assert restored is not None
    assert restored.workspace_state == "cleanup_failed"


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


def test_reconcile_cron_transcript_preserves_existing_followup_messages(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    sid = "cron_job1_1700000600"
    session = Session(
        session_id=sid,
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=250.0,
        messages=[
            {"role": "user", "content": "cron prompt", "timestamp": 100.0},
            {"role": "user", "content": "follow up", "timestamp": 300.0},
            {"role": "assistant", "content": "follow-up answer", "timestamp": 301.0},
        ],
    )
    monkeypatch.setattr("api.models._get_profile_home", lambda _profile: cron_env["home"])
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (sid, "Cron run", "cron", 100.0),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("m-cron-answer", sid, "assistant", "cron answer", 200.0),
        )
        conn.commit()

    assert reconcile_cron_session_transcript(session) is True
    assert [message["content"] for message in session.messages] == [
        "cron prompt",
        "cron answer",
        "follow up",
        "follow-up answer",
    ]


def test_reconcile_cron_transcript_does_not_duplicate_followup_replay(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    sid = "cron_job1_1700000650"
    followup = [
        {"role": "user", "content": "给我一个word", "timestamp": 300.0},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-skill", "function": {"name": "skill_view"}}],
            "timestamp": 301.0,
        },
        {"role": "tool", "tool_call_id": "call-skill", "content": "skill ok", "timestamp": 302.0},
        {"role": "assistant", "content": "Word 文档已生成", "timestamp": 303.0},
    ]
    session = Session(
        session_id=sid,
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=250.0,
        messages=[
            {"role": "user", "content": "cron prompt", "timestamp": 100.0},
            *followup,
        ],
    )
    monkeypatch.setattr("api.models._get_profile_home", lambda _profile: cron_env["home"])
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (sid, "Cron run", "cron", 100.0),
        )
        rows = [
            ("m-cron-answer", sid, "assistant", "cron answer", 200.0),
            ("m-follow-user", sid, "user", "给我一个word", 300.5),
            ("m-follow-assistant-tool", sid, "assistant", "", 301.5),
            ("m-follow-tool", sid, "tool", "skill ok", 302.5),
            ("m-follow-answer", sid, "assistant", "Word 文档已生成", 303.5),
        ]
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()

    assert reconcile_cron_session_transcript(session) is True
    assert [message["content"] for message in session.messages] == [
        "cron prompt",
        "cron answer",
        "给我一个word",
        "",
        "skill ok",
        "Word 文档已生成",
    ]


def test_reconcile_unkeyed_agent_snapshot_replaces_stale_compressed_sidecar(
    cron_env, monkeypatch
):
    """An active Agent snapshot supersedes an unkeyed pre-compression sidecar."""
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    stale_snapshot = [
        {"role": "user", "content": "cron prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "old tool plan", "timestamp": 101.0},
        {"role": "tool", "content": "old result", "timestamp": 102.0},
    ]
    active_snapshot = [
        {
            "role": "user",
            "content": "compressed context",
            "timestamp": 200.0,
            "_hermes_message_class": "context_anchor",
            "_hermes_scaffold_kind": "compaction_summary",
        },
        {"role": "assistant", "content": "retrying tool", "timestamp": 201.0},
        {"role": "tool", "content": "retry result", "timestamp": 202.0},
        {"role": "user", "content": "cron prompt", "timestamp": 203.0},
        {"role": "assistant", "content": "final answer", "timestamp": 204.0},
    ]
    session = Session(
        session_id="cron_job1_1700000675",
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=None,
        messages=stale_snapshot,
    )
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_args, **_kwargs: active_snapshot,
    )

    assert reconcile_cron_session_transcript(session) is True
    assert [message["content"] for message in session.messages] == [
        "compressed context",
        "cron prompt",
        "retrying tool",
        "retry result",
        "final answer",
    ]
    assert session.messages[0].get("_turn_key") is None
    assert session.messages[1]["_turn_key"] == "turn:1"
    assert reconcile_cron_session_transcript(session) is False


def test_reconcile_cron_transcript_uses_output_when_database_reply_missing(cron_env, monkeypatch):
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    session = Session(
        session_id="cron_job1_1700000700",
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        messages=[{"role": "user", "content": "cron prompt", "timestamp": 100.0}],
    )
    monkeypatch.setattr("api.models.get_state_db_session_messages", lambda *_args, **_kwargs: [])
    output = "# Cron Job: Nightly\n\n## Response\n\ncron answer"

    assert reconcile_cron_session_transcript(session, fallback_output=output, run_mtime=200.0) is True
    assert reconcile_cron_session_transcript(session, fallback_output=output, run_mtime=200.0) is False
    assert [message["content"] for message in session.messages] == ["cron prompt", "cron answer"]


@pytest.mark.parametrize(
    "state_prompt",
    [
        "今日热点新闻简报",
        (
            "[IMPORTANT: You are running as a scheduled cron job. "
            "DELIVERY: Your final response will be automatically delivered "
            "to the user — do NOT use send_message or try to deliver "
            "the output yourself. Just produce your report/output as your "
            "final response and the system handles the rest. "
            'SILENT: If there is genuinely nothing new to report, respond with exactly "[SILENT]" '
            "(nothing else) to suppress delivery. Never combine [SILENT] with content — either "
            "report your findings normally, or say [SILENT] and nothing more.]\n\n"
            "今日热点新闻简报"
        ),
    ],
)
def test_reconcile_cron_transcript_consumes_real_user_matching_fallback(
    cron_env, monkeypatch, state_prompt
):
    """A later Agent transcript confirms, rather than duplicates, the fallback prompt."""
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    session = Session(
        session_id="cron_job1_1700000750",
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=200.0,
        messages=[
            {
                "role": "user",
                "content": "今日热点新闻简报",
                "timestamp": 99.0,
                "source": "cron_fallback",
            },
            {
                "role": "assistant",
                "content": "首次失败通知",
                "timestamp": 100.0,
                "source": "cron_fallback",
            },
        ],
    )
    state_messages = [
        {"id": "real-user", "role": "user", "content": state_prompt, "timestamp": 101.0},
        {"id": "real-answer", "role": "assistant", "content": "稍后重试通知", "timestamp": 102.0},
    ]
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_args, **_kwargs: state_messages,
    )

    assert reconcile_cron_session_transcript(session) is True
    assert [message["role"] for message in session.messages] == ["user", "assistant", "assistant"]
    assert [message["content"] for message in session.messages] == [
        "今日热点新闻简报",
        "首次失败通知",
        "稍后重试通知",
    ]
    assert session.messages[0]["source"] == "cron_fallback"
    assert session.messages[2].get("source") is None
    assert reconcile_cron_session_transcript(session) is False


def test_reconcile_cron_transcript_keeps_nonmatching_state_user_and_followup(
    cron_env, monkeypatch
):
    """Only the first matching execution prompt may consume a fallback placeholder."""
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    session = Session(
        session_id="cron_job1_1700000800",
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=200.0,
        messages=[
            {
                "role": "user",
                "content": "cron prompt",
                "timestamp": 99.0,
                "source": "cron_fallback",
            },
            {
                "role": "assistant",
                "content": "fallback answer",
                "timestamp": 100.0,
                "source": "cron_fallback",
            },
            {"role": "user", "content": "cron prompt", "timestamp": 201.0},
        ],
    )
    state_messages = [
        {"id": "real-user", "role": "user", "content": "different cron prompt", "timestamp": 101.0},
        {"id": "real-answer", "role": "assistant", "content": "real answer", "timestamp": 102.0},
    ]
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_args, **_kwargs: state_messages,
    )

    assert reconcile_cron_session_transcript(session) is True
    contents = [message["content"] for message in session.messages]
    assert contents.count("cron prompt") == 2
    assert "different cron prompt" in contents
    assert "real answer" in contents
    assert contents[-1] == "cron prompt"


def test_reconcile_cron_transcript_keeps_matching_webui_followup(
    cron_env, monkeypatch
):
    """A matching execution placeholder must not consume a later WebUI follow-up."""
    from api.models import Session
    from integration.crons.session_bridge import reconcile_cron_session_transcript

    session = Session(
        session_id="cron_job1_1700000850",
        profile="default",
        source_tag="cron",
        cron_execution_profile=str(cron_env["home"]),
        cron_execution_ended_at=200.0,
        messages=[
            {
                "role": "user",
                "content": "cron prompt",
                "timestamp": 99.0,
                "source": "cron_fallback",
            },
            {
                "role": "assistant",
                "content": "fallback answer",
                "timestamp": 100.0,
                "source": "cron_fallback",
            },
            {"role": "user", "content": "cron prompt", "timestamp": 201.0},
        ],
    )
    state_messages = [
        {"id": "real-user", "role": "user", "content": "cron prompt", "timestamp": 101.0},
        {"id": "real-answer", "role": "assistant", "content": "real answer", "timestamp": 102.0},
    ]
    monkeypatch.setattr(
        "api.models.get_state_db_session_messages",
        lambda *_args, **_kwargs: state_messages,
    )

    assert reconcile_cron_session_transcript(session) is True
    assert [message["role"] for message in session.messages].count("user") == 2
    assert [message["content"] for message in session.messages].count("cron prompt") == 2
    assert session.messages[-1]["content"] == "cron prompt"


def _failed_cron_output(detail="Connection error."):
    return f"# Cron Job: Nightly (FAILED)\n\n## Error\n\n```\n{detail}\n```\n"


def test_existing_cron_sidecar_appends_provider_style_error_when_run_failed(cron_env, monkeypatch):
    owner = "default"
    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    sid = "cron_job1_1700000300"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN end_reason TEXT")
        conn.execute(
            "INSERT INTO sessions (id, title, source, started_at, end_reason) VALUES (?, ?, ?, ?, ?)",
            (sid, "Cron failure", "cron", 1700000300.0, "cron_failed"),
        )
        conn.commit()

    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "real", "timestamp": 1.0}]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session

            materialize_cron_session(
                job,
                owner_profile=owner,
                execution_home=cron_env["home"],
                fallback_output=_failed_cron_output(),
                run_mtime=1700000305.0,
            )

    from api.models import Session

    full = Session.load(sid)
    error = full.messages[-1]
    assert error["role"] == "assistant"
    assert error["_error"] is True
    assert error["_error_type"] == "connection_error"
    assert error["provider_details"] == "```\nConnection error.\n```"
    assert error["provider_details_label"] == "技术详情"
    assert full.last_error_at == 1700000305.0


def test_cron_provider_style_error_fallback_is_idempotent(cron_env, monkeypatch):
    owner = "default"
    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    sid = "cron_job1_1700000400"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, "Cron failure", "cron", 1700000400.0))
        conn.commit()
    run = {"session_id": sid, "title": "Cron run", "started_at": 1700000400.0, "ended_at": 1700000405.0, "end_reason": "cron_failed"}
    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "real", "timestamp": 1.0}]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session_run

            materialize_cron_session_run(job, owner_profile=owner, run=run, fallback_output=_failed_cron_output())
            materialize_cron_session_run(job, owner_profile=owner, run=run, fallback_output=_failed_cron_output())

    from api.models import Session

    full = Session.load(sid)
    assert len([message for message in full.messages if message.get("_error")]) == 1


def test_successful_cron_run_does_not_append_error_fallback(cron_env, monkeypatch):
    owner = "default"
    job = {"id": "job1", "name": "Nightly", "profile": "", "prompt": "run nightly"}
    sid = "cron_job1_1700000500"
    with closing(sqlite3.connect(str(cron_env["db"]))) as conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, "Cron success", "cron", 1700000500.0))
        conn.commit()
    run = {"session_id": sid, "title": "Cron run", "started_at": 1700000500.0, "ended_at": 1700000505.0, "end_reason": "cron_complete"}
    with patch("api.models.get_state_db_session_messages", return_value=[{"role": "user", "content": "real", "timestamp": 1.0}]):
        with patch("api.profiles.list_profiles_api", return_value=[{"name": owner, "path": str(cron_env["home"])}]):
            from integration.crons.session_bridge import materialize_cron_session_run

            materialize_cron_session_run(job, owner_profile=owner, run=run, fallback_output="## Response\n\ncompleted")

    from api.models import Session

    full = Session.load(sid)
    assert not any(message.get("_error") for message in full.messages)
