"""Tests for cross-profile cron listing."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_jobs(home: Path, jobs: list):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


@pytest.fixture
def cron_homes(tmp_path, monkeypatch):
    pytest.importorskip("cron.jobs")
    default_home = tmp_path / "default_home"
    alice_home = default_home / "profiles" / "alice"
    bob_home = default_home / "profiles" / "bob"
    _write_jobs(default_home, [{"id": "d1", "name": "default-job"}])
    _write_jobs(alice_home, [{"id": "a1", "name": "alice-job", "profile": "bob"}])
    _write_jobs(bob_home, [{"id": "b1", "name": "bob-job"}])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HERMES_INTEGRATION", "1")

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)

    profiles = [
        {"name": "default", "path": str(default_home)},
        {"name": "alice", "path": str(alice_home)},
        {"name": "bob", "path": str(bob_home)},
    ]

    with patch("api.profiles.list_profiles_api", return_value=profiles):
        yield {
            "default": default_home,
            "alice": alice_home,
            "bob": bob_home,
            "profiles": profiles,
        }


def test_list_jobs_all_profiles_grouped(cron_homes):
    from integration.crons.listing import list_jobs_all_profiles

    payload = list_jobs_all_profiles()
    assert payload["all_profiles"] is True
    by_name = {g["profile"]: g["jobs"] for g in payload["profiles"]}
    assert any(j["id"] == "d1" for j in by_name["default"])
    assert any(j["id"] == "a1" for j in by_name["alice"])
    assert any(j["id"] == "b1" for j in by_name["bob"])
    for jobs in by_name.values():
        for job in jobs:
            assert "storage_profile" not in job


def test_recent_all_profiles_includes_owner(cron_homes, monkeypatch):
    import time

    from integration.crons.listing import recent_completions_all_profiles

    recent_ts = time.time() - 10
    alice_jobs = cron_homes["alice"] / "cron" / "jobs.json"
    data = json.loads(alice_jobs.read_text(encoding="utf-8"))
    data["jobs"][0]["last_run_at"] = recent_ts
    alice_jobs.write_text(json.dumps(data), encoding="utf-8")

    payload = recent_completions_all_profiles(since=time.time() - 60)
    assert any(c["owner_profile"] == "alice" and c["job_id"] == "a1" for c in payload["completions"])
    alice_row = next(c for c in payload["completions"] if c["job_id"] == "a1")
    assert alice_row.get("profile") == "bob"
