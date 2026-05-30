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


def test_integration_create_writes_owner_profile(handler_env, monkeypatch):
    from cron.jobs import list_jobs
    from api.profiles import cron_profile_context_for_home
    from integration.crons.handlers import _handle_create

    handler = MagicMock()
    body = {
        "owner_profile": "ops",
        "name": "test",
        "schedule": "every 1h",
        "prompt": "ping",
    }

    _handle_create(handler, body)

    with cron_profile_context_for_home(handler_env):
        jobs = list_jobs(include_disabled=True)
    assert any(j.get("name") == "test" for j in jobs)
