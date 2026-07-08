"""GET /api/crons?profile= lists jobs from a named profile store."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler: _JSONHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _install_cron_jobs(monkeypatch, jobs_by_home, current_home):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def _list_jobs(include_disabled=True):
        return [dict(job) for job in jobs_by_home[current_home["value"]]]

    cron_jobs.list_jobs = _list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_crons_list_profile_query_pins_named_profile(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    current_home = {"value": None}
    jobs_by_home = {
        "alpha-home": [{"id": "alpha-job", "name": "Alpha"}],
        "beta-home": [{"id": "beta-job", "name": "Beta"}],
    }
    _install_cron_jobs(monkeypatch, jobs_by_home, current_home)

    class _Ctx:
        def __init__(self, home):
            self.home = str(home)
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = self.home
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [
            {"name": "alpha", "visible": True},
            {"name": "beta", "visible": True},
        ],
    )
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: Path({"alpha": "alpha-home", "beta": "beta-home"}[name]),
    )
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    handler = _JSONHandler()
    assert (
        routes.handle_get(
            handler, SimpleNamespace(path="/api/crons", query="profile=beta")
        )
        is not False
    )
    body = _payload(handler)

    assert handler.status == 200
    assert [job["id"] for job in body["jobs"]] == ["beta-job"]
    assert current_home["value"] is None


def test_crons_list_unknown_profile_returns_400(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "alpha", "visible": True}],
    )

    handler = _JSONHandler()
    assert (
        routes.handle_get(
            handler, SimpleNamespace(path="/api/crons", query="profile=missing")
        )
        is not False
    )
    body = _payload(handler)

    assert handler.status == 400
    assert "Unknown profile" in body.get("error", "")
