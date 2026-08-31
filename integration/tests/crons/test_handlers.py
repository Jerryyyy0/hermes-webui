"""Tests for /api/integration/crons/* handlers."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _write_jobs(home: Path, jobs: list):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


@pytest.fixture
def handler_env(tmp_path, monkeypatch):
    pytest.importorskip("cron.jobs")
    home = tmp_path / "hermes"
    _write_jobs(home, [])
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_DEFAULT_MODEL", raising=False)

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)

    profiles = [{"name": "ops", "path": str(home / "profiles" / "ops")}]
    (home / "profiles" / "ops").mkdir(parents=True)
    (home / "profiles" / "ops" / "config.yaml").write_text(
        "model:\n  default: test/default-model\n", encoding="utf-8"
    )
    _write_jobs(home / "profiles" / "ops", [])

    with patch("api.profiles.list_profiles_api", return_value=profiles):
        with patch("api.routes._available_cron_profile_names", return_value={"default", "ops"}):
            yield home / "profiles" / "ops"


def test_integration_create_uses_single_profile_for_owner_and_execution(handler_env, monkeypatch):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    body = {
        "profile": "ops",
        "name": "test",
        "schedule": "every 1h",
        "prompt": "ping",
        "skills": ["daily-summary"],
    }

    _handle_create(handler, body)

    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert any(j.get("name") == "test" for j in jobs)
    assert any(j.get("name") == "test" and j.get("profile") == "ops" for j in jobs)
    assert any(j.get("name") == "test" and j.get("skills") == ["daily-summary"] for j in jobs)
    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert payload["profile"] == "ops"


def test_integration_create_persists_idle_window_and_returns_it(handler_env):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    idle_window = {
        "start_schedule": {"kind": "cron", "expr": "0 22 * * *", "display": "每天 22:00"},
        "end_schedule": {"kind": "cron", "expr": "0 6 * * *", "display": "每天 06:00"},
    }

    _handle_create(
        handler,
        {
            "profile": "ops",
            "schedule": "every 1h",
            "prompt": "ping",
            "idle_window": idle_window,
        },
    )

    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert jobs[0]["idle_window"] == idle_window
    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert payload["job"]["idle_window"] == idle_window


def test_integration_create_defaults_idle_window_to_null(handler_env):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    _handle_create(handler, {"profile": "ops", "schedule": "every 1h", "prompt": "ping"})

    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert jobs[0]["idle_window"] is None
    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert payload["job"]["idle_window"] is None


@pytest.mark.parametrize(
    "idle_window",
    [
        {"start_schedule": {"kind": "cron", "expr": "0 22 * * *"}},
        {
            "start_schedule": {"kind": "once", "run_at": "2026-09-01T22:00:00+08:00"},
            "end_schedule": {"kind": "cron", "expr": "0 6 * * *"},
        },
    ],
)
def test_integration_create_rejects_invalid_idle_window(handler_env, idle_window):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    _handle_create(
        handler,
        {
            "profile": "ops",
            "schedule": "every 1h",
            "prompt": "ping",
            "idle_window": idle_window,
        },
    )

    handler.send_response.assert_called_with(400)
    with cron_profile_context_for_home(handler_env):
        assert list_jobs(include_disabled=True) == []


def test_integration_update_replaces_and_clears_idle_window(handler_env):
    from integration.crons.handlers import _handle_update

    handler = MagicMock()
    idle_window = {
        "start_schedule": {"kind": "cron", "expr": "0 22 * * *", "display": "每天 22:00"},
        "end_schedule": {"kind": "cron", "expr": "0 6 * * *", "display": "每天 06:00"},
    }
    with patch(
        "cron.jobs.update_job",
        return_value={"id": "job1", "idle_window": idle_window},
    ) as update_job:
        _handle_update(
            handler,
            {"profile": "ops", "job_id": "job1", "idle_window": idle_window},
        )
    assert update_job.call_args.args == ("job1", {"profile": "ops", "idle_window": idle_window})

    handler = MagicMock()
    with patch(
        "cron.jobs.update_job",
        return_value={"id": "job1", "idle_window": None},
    ) as update_job:
        _handle_update(
            handler,
            {"profile": "ops", "job_id": "job1", "idle_window": None},
        )
    assert update_job.call_args.args == ("job1", {"profile": "ops", "idle_window": None})


def test_integration_create_allows_missing_model_like_core_api(handler_env):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    (handler_env / "config.yaml").unlink()
    handler = MagicMock()
    _handle_create(
        handler,
        {"profile": "ops", "schedule": "every 1h", "prompt": "ping"},
    )

    handler.send_response.assert_called_with(200)
    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0]["model"] is None
    assert jobs[0]["workspace_policy"]["version"] == 1
    assert jobs[0]["workspace_policy"]["strategy"] == "managed"


def test_integration_create_accepts_explicit_model_without_profile_default(handler_env):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    (handler_env / "config.yaml").unlink()
    handler = MagicMock()
    _handle_create(
        handler,
        {
            "profile": "ops",
            "schedule": "every 1h",
            "prompt": "ping",
            "model": "test/pinned-model",
        },
    )

    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0]["model"] == "test/pinned-model"


def test_integration_create_requires_profile(handler_env):
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    body = {
        "owner_profile": "ops",
        "name": "test",
        "schedule": "every 1h",
        "prompt": "ping",
    }

    _handle_create(handler, body)

    handler.send_response.assert_called_with(400)
    assert b"owner_profile is not supported" in handler.wfile.write.call_args.args[0]


def test_integration_create_rejects_owner_profile_field(handler_env):
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    body = {
        "owner_profile": "default",
        "profile": "ops",
        "name": "test",
        "schedule": "every 1h",
        "prompt": "ping",
    }

    _handle_create(handler, body)

    handler.send_response.assert_called_with(400)
    assert b"owner_profile is not supported" in handler.wfile.write.call_args.args[0]


def test_integration_update_allows_model_less_job_like_core_api(handler_env):
    from integration.crons.handlers import _handle_update

    handler = MagicMock()
    updated = {"id": "job1", "name": "after", "model": None}
    with patch("cron.jobs.update_job", return_value=updated) as update_job:
        _handle_update(
            handler,
            {"profile": "ops", "job_id": "job1", "name": "after"},
        )

    handler.send_response.assert_called_with(200)
    update_job.assert_called_once()


def test_integration_run_starts_model_less_job_like_core_api(handler_env):
    from integration.crons.handlers import _handle_run

    handler = MagicMock()
    job = {"id": "job1", "name": "test", "model": None}
    execution_job = {"id": "job1", "name": "test", "model": "discovered-model", "profile": "ops"}
    with patch("cron.jobs.get_job", return_value=job):
        with patch("api.routes._is_cron_running", return_value=(False, 0)):
            with patch("api.routes._mark_cron_running") as mark_running:
                with patch(
                    "integration.crons.execution_model.prepare_cron_hub_execution_job",
                    return_value=execution_job,
                ) as prepare:
                    with patch("integration.crons.handlers.threading.Thread") as thread:
                        _handle_run(handler, {"profile": "ops", "job_id": "job1"})

    mark_running.assert_called_once_with("job1")
    prepare.assert_called_once_with({**job, "profile": "ops"}, "ops", handler_env)
    thread.return_value.start.assert_called_once_with()
    run_job = thread.call_args.kwargs["args"][0]
    assert run_job["model"] == "discovered-model"
    assert job["model"] is None
    assert run_job["profile"] == "ops"
    assert b'"status": "running"' in handler.wfile.write.call_args.args[0]


def test_integration_run_no_agent_job_does_not_require_model(handler_env):
    from integration.crons.handlers import _handle_run

    (handler_env / "config.yaml").unlink()
    handler = MagicMock()
    job = {"id": "job1", "name": "script", "model": None, "no_agent": True}
    with patch("cron.jobs.get_job", return_value=job):
        with patch("api.routes._is_cron_running", return_value=(False, 0)):
            with patch("api.routes._mark_cron_running") as mark_running:
                with patch("integration.crons.handlers.threading.Thread") as thread:
                    _handle_run(handler, {"profile": "ops", "job_id": "job1"})

    mark_running.assert_called_once_with("job1")
    thread.return_value.start.assert_called_once_with()


def test_integration_delete_cleans_job_history(handler_env):
    from integration.crons.handlers import _handle_delete

    handler = MagicMock()
    job = {"id": "job1", "name": "test", "profile": "ops"}
    cleanup = {"ok": True, "deleted": True, "deleted_output_files": 2}
    with patch("cron.jobs.get_job", return_value=job):
        with patch("cron.jobs.remove_job", return_value=True):
            with patch(
                "integration.crons.session_bridge.delete_cron_job_history",
                return_value=cleanup,
            ) as delete_history:
                _handle_delete(handler, {"profile": "ops", "job_id": "job1"})

    delete_history.assert_called_once_with("job1", owner_profile="ops", job=job)
    assert b'"history_cleanup"' in handler.wfile.write.call_args.args[0]


def test_integration_resume_returns_actionable_error_for_past_one_shot_job(handler_env):
    from integration.crons.handlers import _handle_resume

    handler = MagicMock()
    error = (
        "Cannot resume: one-shot time 2026-07-30T15:57:58.524558+08:00 "
        "is in the past (grace window: 120s) and will never fire."
    )
    with patch("cron.jobs.resume_job", side_effect=ValueError(error)):
        _handle_resume(handler, {"profile": "ops", "job_id": "job1"})

    handler.send_response.assert_called_with(400)
    payload = json.loads(handler.wfile.write.call_args.args[0].decode("utf-8"))
    assert payload == {"error": "执行时间是历史时间，请修改执行时间后启用"}
