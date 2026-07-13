from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from integration.gateway_startup import startup


@pytest.fixture(autouse=True)
def reset_startup(monkeypatch):
    startup._reset_startup_state_for_tests()
    monkeypatch.delenv("HERMES_WEBUI_START_PROFILE_GATEWAYS", raising=False)
    monkeypatch.setattr(startup, "_multiplex_enabled", lambda _home: False)
    yield
    startup._reset_startup_state_for_tests()


def profiles():
    return [
        {"name": "default", "path": "/tmp/hermes", "is_default": True, "gateway_running": False},
        {"name": "abc", "path": "/tmp/hermes/profiles/abc", "is_default": False, "gateway_running": False},
    ]


def test_default_on_starts_all_profiles():
    seen = []

    result = startup.ensure_all_profile_gateways(
        list_profiles=profiles,
        start_profile=lambda profile: seen.append(profile["name"]) or {"profile": profile["name"], "status": "started"},
    )

    assert sorted(seen) == ["abc", "default"]
    assert result["counts"] == {"started": 2}


def test_explicit_disable_skips_enumeration(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_START_PROFILE_GATEWAYS", "0")

    result = startup.ensure_all_profile_gateways(list_profiles=lambda: pytest.fail("must not enumerate"))

    assert result == {"enabled": False, "results": []}


def test_multiplex_starts_only_default(monkeypatch):
    monkeypatch.setattr(startup, "_multiplex_enabled", lambda _home: True)
    seen = []

    result = startup.ensure_all_profile_gateways(
        list_profiles=profiles,
        start_profile=lambda profile: seen.append(profile["name"]) or {"profile": profile["name"], "status": "started"},
    )

    assert seen == ["default"]
    assert {row["profile"]: row["status"] for row in result["results"]} == {
        "default": "started",
        "abc": "skipped_multiplex",
    }


def test_failure_does_not_prevent_other_profiles():
    def start(profile):
        if profile["name"] == "abc":
            raise RuntimeError("secret detail")
        return {"profile": profile["name"], "status": "started"}

    result = startup.ensure_all_profile_gateways(list_profiles=profiles, start_profile=start)

    assert sorted(row["status"] for row in result["results"]) == ["failed", "started"]
    assert "secret detail" not in repr(result)


def test_only_runs_once_per_process():
    calls = []
    kwargs = {
        "list_profiles": profiles,
        "start_profile": lambda profile: calls.append(profile["name"]) or {"profile": profile["name"], "status": "started"},
    }

    startup.ensure_all_profile_gateways(**kwargs)
    second = startup.ensure_all_profile_gateways(**kwargs)

    assert len(calls) == 2
    assert second["already_invoked"] is True


def test_start_uses_verified_runtime_and_does_not_mutate_home(monkeypatch):
    from api import agent_cli_runtime

    original_home = os.environ.get("HERMES_HOME")
    verified_env = {"PYTHONUTF8": "1"}
    invocation = agent_cli_runtime.AgentCliInvocation(
        ("/hermes",), str(Path.home()), verified_env, "managed_launcher"
    )
    monkeypatch.setattr(agent_cli_runtime, "resolve_agent_cli_runtime", lambda: invocation)
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = startup._start_profile_gateway(profiles()[0], runner=runner)

    assert result["status"] == "started"
    assert captured["command"] == ["/hermes", "gateway", "start"]
    assert captured["cwd"] == str(Path.home())
    assert captured["env"] is verified_env
    assert os.environ.get("HERMES_HOME") == original_home


def test_timeout_is_classified(monkeypatch):
    from api import agent_cli_runtime

    monkeypatch.setattr(
        agent_cli_runtime,
        "resolve_agent_cli_runtime",
        lambda: agent_cli_runtime.AgentCliInvocation(("/hermes",), str(Path.home()), {}, "launcher"),
    )

    def runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("hermes", 1)

    result = startup._start_profile_gateway(profiles()[0], runner=runner)

    assert result == {"profile": "default", "status": "timed_out"}


def test_runtime_unavailable_is_classified(monkeypatch):
    from api import agent_cli_runtime

    def unavailable():
        raise agent_cli_runtime.AgentCliRuntimeUnavailable("No verified runtime")

    monkeypatch.setattr(agent_cli_runtime, "resolve_agent_cli_runtime", unavailable)

    result = startup._start_profile_gateway(profiles()[0])

    assert result["status"] == "runtime_unavailable"
    assert result["profile"] == "default"


def test_already_running_skips_command():
    profile = profiles()[0]
    profile["gateway_running"] = True

    result = startup._start_profile_gateway(profile, runner=lambda *_a, **_k: pytest.fail("must not run"))

    assert result == {"profile": "default", "status": "already_running"}
