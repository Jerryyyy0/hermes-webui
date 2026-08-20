"""Regression tests for manual WebUI cron runs."""

import copy
import multiprocessing


def _install_cron_fakes(monkeypatch, calls, deliver_result=None, silent_marker="[SILENT]"):
    cron_jobs = type("CronJobs", (), {})()
    cron_jobs.save_job_output = lambda job_id, output: calls.append(
        ("save", job_id, output)
    )
    cron_jobs.mark_job_run = lambda job_id, success, error=None, delivery_error=None: calls.append(
        ("mark", job_id, success, error, delivery_error)
    )

    cron_scheduler = type("CronScheduler", (), {})()
    cron_scheduler.SILENT_MARKER = silent_marker
    if deliver_result is None:
        deliver_result = lambda job, content: calls.append(
            ("deliver", job["id"], content)
        ) or None
    cron_scheduler._deliver_result = deliver_result

    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(__import__("sys").modules, "cron.scheduler", cron_scheduler)


def test_manual_cron_run_saves_output_delivers_and_marks_job(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "done", None),
    )

    routes._mark_cron_running("job123")
    routes._run_cron_tracked({"id": "job123"})

    assert calls == [
        ("save", "job123", "manual output"),
        ("deliver", "job123", "done"),
        ("mark", "job123", True, None, None),
    ]
    assert routes._is_cron_running("job123") == (False, 0.0)


def test_manual_cron_run_marks_empty_response_as_failure_without_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "", None),
    )

    routes._mark_cron_running("job-empty")
    routes._run_cron_tracked({"id": "job-empty"})

    assert calls[0] == ("save", "job-empty", "manual output")
    assert calls[1][0:3] == ("mark", "job-empty", False)
    assert "empty response" in calls[1][3]
    assert calls[1][4] is None
    assert routes._is_cron_running("job-empty") == (False, 0.0)


def test_manual_cron_run_records_delivery_errors_separately(monkeypatch):
    import api.routes as routes

    calls = []

    def fail_delivery(job, content):
        calls.append(("deliver", job["id"], content))
        return "discord not configured"

    _install_cron_fakes(monkeypatch, calls, deliver_result=fail_delivery)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "done", None),
    )

    routes._mark_cron_running("job-delivery-error")
    routes._run_cron_tracked({"id": "job-delivery-error"})

    assert calls == [
        ("save", "job-delivery-error", "manual output"),
        ("deliver", "job-delivery-error", "done"),
        ("mark", "job-delivery-error", True, None, "discord not configured"),
    ]
    assert routes._is_cron_running("job-delivery-error") == (False, 0.0)


def test_manual_cron_run_skips_silent_success_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "[SILENT]", None),
    )

    routes._mark_cron_running("job-silent")
    routes._run_cron_tracked({"id": "job-silent"})

    assert calls == [
        ("save", "job-silent", "manual output"),
        ("mark", "job-silent", True, None, None),
    ]
    assert routes._is_cron_running("job-silent") == (False, 0.0)


def test_manual_cron_run_delivers_failure_notice(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (False, "manual output", "", "boom"),
    )

    routes._mark_cron_running("job-failed")
    routes._run_cron_tracked({"id": "job-failed", "name": "Nightly check"})

    assert calls[0] == ("save", "job-failed", "manual output")
    assert calls[1][0:2] == ("deliver", "job-failed")
    assert "Nightly check" in calls[1][2]
    assert "boom" in calls[1][2]
    assert calls[2] == ("mark", "job-failed", False, "boom", None)
    assert routes._is_cron_running("job-failed") == (False, 0.0)


def test_manual_cron_failure_forwards_result_to_materializer(monkeypatch):
    import api.routes as routes
    import integration.crons.hooks as hooks

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    result = (False, "", "", "no_agent=True but no script is set for this job")
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: result,
    )
    monkeypatch.setattr(
        hooks,
        "materialize_after_cron_run",
        lambda job, **kwargs: calls.append(("materialize", kwargs.get("execution_result"))),
    )

    routes._mark_cron_running("job-missing-script")
    routes._run_cron_tracked({"id": "job-missing-script", "no_agent": True})

    assert ("materialize", result) in calls


def test_manual_cron_subprocess_returns_v1_workspace_handoff_to_parent(monkeypatch, tmp_path):
    """V1 allocation happens in the spawned child, not the parent job copy."""
    import api.routes as routes

    class FakeQueue:
        def __init__(self):
            self.items = []

        def put(self, value):
            self.items.append(value)

        def get(self, timeout=None):
            return self.items.pop(0)

        def close(self):
            pass

        def join_thread(self):
            pass

    class FakeProcess:
        def __init__(self, *, target, args):
            self._target = target
            # Model ``spawn``: mutation in the child cannot alter the original job.
            self._args = (copy.deepcopy(args[0]), *args[1:])
            self.exitcode = 0

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

        def terminate(self):
            raise AssertionError("successful fake child must not be terminated")

    class FakeContext:
        def __init__(self):
            self.queue = FakeQueue()

        def Queue(self, maxsize):
            return self.queue

        def Process(self, *, target, args):
            return FakeProcess(target=target, args=args)

    workspace = tmp_path / "workspace" / "sessions" / "cron" / "default" / "cron_job1_run1"
    workspace.mkdir(parents=True)
    def fake_subprocess_main(child_job, _execution_profile_home, result_queue):
        child_job.update(
            {
                "_cron_execution_workspace": str(workspace),
                "_cron_workspace_strategy": "managed",
                "_cron_workspace_namespace_base": str(workspace.parent),
            }
        )
        result_queue.put(
            (
                "ok",
                (True, "output", "final", None),
                routes._cron_execution_handoff(child_job),
            )
        )

    monkeypatch.setattr(routes, "_cron_job_subprocess_main", fake_subprocess_main)
    monkeypatch.setattr(multiprocessing, "get_context", lambda name: FakeContext())

    job = {
        "id": "job1",
        "_cron_session_id": "cron_job1_run1",
        "workspace_policy": {"version": 1, "strategy": "managed"},
    }
    assert routes._run_cron_job_in_profile_subprocess(job, None) == (
        True,
        "output",
        "final",
        None,
    )
    assert job["_cron_execution_workspace"] == str(workspace)
    assert job["_cron_workspace_strategy"] == "managed"
    assert job["_cron_workspace_namespace_base"] == str(workspace.parent)


def test_manual_cron_subprocess_rejects_mismatched_workspace_handoff():
    import api.routes as routes

    job = {"_cron_session_id": "cron_job1_expected"}
    routes._apply_cron_execution_handoff(
        job,
        {
            "_cron_session_id": "cron_job1_other",
            "_cron_execution_workspace": "/tmp/untrusted-workspace",
        },
    )

    assert "_cron_execution_workspace" not in job
