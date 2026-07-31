"""Tests for integration cron hooks (preserve one-shot jobs)."""

from __future__ import annotations

import copy
import sys
import types
from datetime import datetime, timezone

import pytest


def _register_fake_cron_jobs(monkeypatch, fake_mod: types.ModuleType) -> None:
    """Replace any cached Hermes cron modules with the in-memory fake."""
    monkeypatch.delitem(sys.modules, "cron.jobs", raising=False)
    cron_parent = sys.modules.get("cron")
    if cron_parent is None or not isinstance(cron_parent, types.ModuleType):
        cron_parent = types.ModuleType("cron")
    monkeypatch.setitem(sys.modules, "cron", cron_parent)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_mod)
    cron_parent.jobs = fake_mod


def _make_fake_cron_jobs(jobs: list[dict]):
    """Build a minimal cron.jobs fake that deletes jobs when repeat limit is hit."""
    store = copy.deepcopy(jobs)

    class _FakeNow:
        @staticmethod
        def isoformat():
            return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    mod = types.ModuleType("cron.jobs")

    def load_jobs():
        return copy.deepcopy(store)

    def save_jobs(new_jobs):
        store.clear()
        store.extend(copy.deepcopy(new_jobs))

    def mark_job_run(job_id, success, error=None, delivery_error=None):
        jobs_local = load_jobs()
        for i, job in enumerate(jobs_local):
            if job.get("id") != job_id:
                continue
            job["last_run_at"] = _FakeNow.isoformat()
            job["last_status"] = "ok" if success else "error"
            job["last_error"] = error if not success else None
            job["last_delivery_error"] = delivery_error
            if job.get("repeat"):
                job["repeat"]["completed"] = job["repeat"].get("completed", 0) + 1
                times = job["repeat"].get("times")
                completed = job["repeat"]["completed"]
                if times is not None and times > 0 and completed >= times:
                    jobs_local.pop(i)
                    save_jobs(jobs_local)
                    return
            jobs_local[i] = job
            save_jobs(jobs_local)
            return

    mod.load_jobs = load_jobs
    mod.save_jobs = save_jobs
    mod.mark_job_run = mark_job_run
    mod._hermes_now = _FakeNow
    mod._jobs_file_lock = type("Lock", (), {"__enter__": lambda self: None, "__exit__": lambda *a: None})()
    return mod, store


@pytest.fixture
def integration_cron_env(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    import integration.crons.hooks as hooks

    monkeypatch.setattr(hooks, "_installed", False)
    return hooks


def test_cron_repeat_limit_will_delete_detects_final_run():
    from integration.crons.hooks import _cron_repeat_limit_will_delete

    assert _cron_repeat_limit_will_delete(
        {"repeat": {"times": 1, "completed": 0}}
    )
    assert not _cron_repeat_limit_will_delete(
        {"repeat": {"times": 3, "completed": 1}}
    )
    assert not _cron_repeat_limit_will_delete(
        {"repeat": {"times": None, "completed": 5}}
    )
    assert not _cron_repeat_limit_will_delete({})


def test_preserve_once_job_kept_after_repeat_limit(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    once_job = {
        "id": "once1",
        "name": "One shot",
        "schedule": {"kind": "once"},
        "repeat": {"times": 1, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    fake_mod, store = _make_fake_cron_jobs([once_job])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    hooks._install_preserve_once_cron_hook()

    sys.modules["cron.jobs"].mark_job_run("once1", True)

    assert len(store) == 1
    job = store[0]
    assert job["id"] == "once1"
    assert job["enabled"] is False
    assert job["state"] == "completed"
    assert job["next_run_at"] is None
    assert job["last_status"] == "ok"
    assert job["repeat"]["completed"] == 1


def test_preserve_once_job_records_failure(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    once_job = {
        "id": "once_fail",
        "name": "Fail shot",
        "schedule": {"kind": "once"},
        "repeat": {"times": 1, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    fake_mod, store = _make_fake_cron_jobs([once_job])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    hooks._install_preserve_once_cron_hook()
    sys.modules["cron.jobs"].mark_job_run(
        "once_fail", False, error="boom", delivery_error="discord down"
    )

    assert len(store) == 1
    job = store[0]
    assert job["last_status"] == "error"
    assert job["last_error"] == "boom"
    assert job["last_delivery_error"] == "discord down"
    assert job["state"] == "completed"
    assert job["enabled"] is False


def test_non_terminal_repeat_still_updates_without_restore(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    recurring_limited = {
        "id": "lim3",
        "name": "Three runs",
        "schedule": {"kind": "interval"},
        "repeat": {"times": 3, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    fake_mod, store = _make_fake_cron_jobs([recurring_limited])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    hooks._install_preserve_once_cron_hook()
    sys.modules["cron.jobs"].mark_job_run("lim3", True)

    assert len(store) == 1
    assert store[0]["repeat"]["completed"] == 1
    assert store[0]["enabled"] is True


def test_infinite_repeat_delegates_to_original(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    forever_job = {
        "id": "forever",
        "name": "Forever",
        "schedule": {"kind": "interval"},
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    fake_mod, store = _make_fake_cron_jobs([forever_job])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    hooks._install_preserve_once_cron_hook()
    sys.modules["cron.jobs"].mark_job_run("forever", True)

    assert len(store) == 1
    assert store[0]["repeat"]["completed"] == 1
    assert store[0]["last_status"] == "ok"
    assert store[0]["enabled"] is True


def test_install_hook_is_idempotent(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    fake_mod, _store = _make_fake_cron_jobs([])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    hooks._install_preserve_once_cron_hook()
    first = sys.modules["cron.jobs"].mark_job_run
    hooks._install_preserve_once_cron_hook()
    assert sys.modules["cron.jobs"].mark_job_run is first


def test_preserve_hook_patches_scheduler_cached_mark_job_run(integration_cron_env, monkeypatch):
    hooks = integration_cron_env
    once_job = {
        "id": "cached",
        "name": "Cached",
        "schedule": {"kind": "once"},
        "repeat": {"times": 1, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    fake_mod, store = _make_fake_cron_jobs([once_job])
    _register_fake_cron_jobs(monkeypatch, fake_mod)

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.mark_job_run = fake_mod.mark_job_run
    sys.modules["cron"].scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    hooks._install_preserve_once_cron_hook()
    scheduler.mark_job_run("cached", True)

    assert len(store) == 1
    assert store[0]["id"] == "cached"
    assert store[0]["state"] == "completed"
    assert store[0]["enabled"] is False


def test_materialize_hook_uses_owner_resolved_before_run(monkeypatch):
    import api.profiles as profiles
    import integration.crons.hooks as hooks
    import integration.crons.listing as listing

    class DummyContext:
        def __init__(self, _home):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    scheduler = types.ModuleType("cron.scheduler")
    calls = []

    def run_job(job):
        calls.append(("run", job["id"]))
        return True, "output", "done", None

    scheduler.run_job = run_job
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    cron_parent = sys.modules.get("cron") or types.ModuleType("cron")
    cron_parent.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_parent)

    monkeypatch.setattr(profiles, "_home_for_scheduled_cron_job", lambda job: "home")
    monkeypatch.setattr(profiles, "_cron_profile_context_depth", lambda: 0)
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", DummyContext)
    monkeypatch.setattr(listing, "resolve_owner_profile_for_job", lambda job_id: "default")
    monkeypatch.setattr(
        hooks,
        "materialize_after_cron_run",
        lambda job, **kw: calls.append(
            ("materialize", job["id"], kw.get("owner_profile"), kw.get("execution_result"))
        ),
    )

    hooks._install_run_job_materialize_hook()
    scheduler.run_job({"id": "deleted-during-run"})

    assert calls == [
        ("run", "deleted-during-run"),
        ("materialize", "deleted-during-run", "default", (True, "output", "done", None)),
    ]


def test_materialize_after_no_agent_failure_passes_scheduler_error(monkeypatch, tmp_path):
    import integration.crons.hooks as hooks
    import integration.crons.listing as listing
    import integration.crons.session_bridge as session_bridge

    captured = {}
    monkeypatch.setattr(listing, "resolve_owner_profile_for_job", lambda _job_id: "default")
    monkeypatch.setattr(
        session_bridge,
        "read_cron_output_for_run",
        lambda _job_id: ("script output", "2026-07-31_12-00-05.md"),
    )
    monkeypatch.setattr(
        session_bridge,
        "materialize_cron_session",
        lambda _job, **kwargs: captured.update(kwargs) or None,
    )

    assert hooks.materialize_after_cron_run(
        {"id": "script1", "name": "Watchdog", "no_agent": True},
        execution_home=tmp_path,
        execution_result=(False, "cron output", "delivery alert", "Script not found: /tmp/watchdog.sh"),
    ) is None

    assert captured["execution_end_reason"] == "cron_error"
    assert captured["execution_error_detail"] == "Script not found: /tmp/watchdog.sh"


def test_scheduled_no_agent_failure_gets_runtime_session_id(monkeypatch):
    import api.profiles as profiles
    import integration.crons.hooks as hooks
    import integration.crons.listing as listing

    class DummyContext:
        def __init__(self, _home):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_job = lambda _job: (False, "", "", "no_agent=True but no script is set for this job")
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    cron_parent = sys.modules.get("cron") or types.ModuleType("cron")
    cron_parent.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_parent)
    monkeypatch.setattr(profiles, "_home_for_scheduled_cron_job", lambda _job: "home")
    monkeypatch.setattr(profiles, "_cron_profile_context_depth", lambda: 0)
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", DummyContext)
    monkeypatch.setattr(listing, "resolve_owner_profile_for_job", lambda _job_id: "default")
    captured = {}
    monkeypatch.setattr(
        hooks,
        "materialize_after_cron_run",
        lambda _job, **kwargs: captured.update(kwargs),
    )

    hooks._install_run_job_materialize_hook()
    scheduler.run_job({"id": "script1", "no_agent": True})

    assert captured["execution_result"][0] is False
    assert captured["session_id"].startswith("cron_script1_")


def test_install_hooks_materialize_unconditional_without_integration(monkeypatch):
    """install_cron_integration_hooks() must install the materialize hook even
    when HERMES_INTEGRATION is off — cron sessions need sidecars + turn_artifacts
    persistence regardless of integration mode. Only preserve-once stays gated."""
    monkeypatch.delenv("HERMES_INTEGRATION", raising=False)

    import integration.crons.hooks as hooks

    monkeypatch.setattr(hooks, "_installed", False)

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_job = lambda job: (True, "out", "done", None)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    cron_parent = sys.modules.get("cron") or types.ModuleType("cron")
    cron_parent.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron_parent)

    materialize_installed = {"called": False}
    orig_install_materialize = hooks._install_run_job_materialize_hook

    def _spy_materialize():
        materialize_installed["called"] = True
        orig_install_materialize()

    monkeypatch.setattr(hooks, "_install_run_job_materialize_hook", _spy_materialize)

    preserve_installed = {"called": False}
    monkeypatch.setattr(
        hooks,
        "_install_preserve_once_cron_hook",
        lambda: preserve_installed.__setitem__("called", True),
    )

    hooks.install_cron_integration_hooks()

    assert materialize_installed["called"] is True
    assert preserve_installed["called"] is False
