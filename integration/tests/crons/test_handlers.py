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

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)

    profiles = [{"name": "ops", "path": str(home / "profiles" / "ops")}]
    (home / "profiles" / "ops").mkdir(parents=True)
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
