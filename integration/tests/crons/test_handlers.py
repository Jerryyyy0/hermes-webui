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
