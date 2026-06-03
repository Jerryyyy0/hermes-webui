"""Tests for Cron Hub unread run tracking."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _write_jobs(home: Path, jobs: list):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _write_run(home: Path, job_id: str, filename: str, modified: float):
    out_dir = home / "cron" / "output" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text("## Response\nok\n", encoding="utf-8")
    os.utime(path, (modified, modified))
    return path


@pytest.fixture
def unread_env(tmp_path, monkeypatch):
    pytest.importorskip("cron.jobs")
    home = tmp_path / "hermes"
    ops_home = home / "profiles" / "ops"
    _write_jobs(home, [])
    _write_jobs(ops_home, [{"id": "job1", "name": "Daily report"}])

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INTEGRATION", "1")

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    profiles = [{"name": "ops", "path": str(ops_home)}]
    with patch("api.profiles.list_profiles_api", return_value=profiles):
        yield {"home": home, "ops": ops_home}


def test_unread_summary_counts_run_files(unread_env):
    from integration.crons.unread import unread_summary_all_profiles

    now = time.time()
    _write_run(unread_env["ops"], "job1", "one.md", now - 20)
    _write_run(unread_env["ops"], "job1", "two.md", now - 10)

    payload = unread_summary_all_profiles()

    assert payload["unread_count"] == 2
    assert payload["jobs"][0]["profile"] == "ops"
    assert payload["jobs"][0]["job_id"] == "job1"
    assert payload["jobs"][0]["unread_count"] == 2
    assert payload["jobs"][0]["latest_filename"] == "two.md"


def test_mark_job_read_advances_cursor_only_to_current_runs(unread_env):
    from integration.crons.unread import mark_job_read, unread_summary_all_profiles

    now = time.time()
    _write_run(unread_env["ops"], "job1", "one.md", now - 20)
    _write_run(unread_env["ops"], "job1", "two.md", now - 10)

    result = mark_job_read("ops", "job1")

    assert result["ok"] is True
    assert unread_summary_all_profiles()["unread_count"] == 0

    _write_run(unread_env["ops"], "job1", "three.md", now + 10)
    payload = unread_summary_all_profiles()

    assert payload["unread_count"] == 1
    assert payload["jobs"][0]["latest_filename"] == "three.md"


def test_unread_handlers_route_get_and_mark_read(unread_env):
    from integration.crons.handlers import try_handle_get, try_handle_post

    now = time.time()
    _write_run(unread_env["ops"], "job1", "one.md", now - 10)

    handler = MagicMock()
    handled = try_handle_get(handler, SimpleNamespace(path="/api/integration/crons/unread"))

    assert handled is True
    assert b'"unread_count": 1' in handler.wfile.write.call_args.args[0]

    handler = MagicMock()
    handled = try_handle_post(
        handler,
        SimpleNamespace(path="/api/integration/crons/unread/read"),
        {"profile": "ops", "job_id": "job1"},
    )

    assert handled is True
    assert b'"ok": true' in handler.wfile.write.call_args.args[0]
