from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from integration.gateway_startup import startup


@pytest.fixture(autouse=True)
def reset_startup(monkeypatch):
    startup._reset_startup_state_for_tests()
    monkeypatch.delenv("HERMES_WEBUI_START_PROFILE_GATEWAYS", raising=False)
    monkeypatch.setattr(startup, "_running_in_container", lambda: False)
    monkeypatch.setattr(startup, "_s6_service_manager_available", lambda: False)
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


def test_plain_container_uses_webui_gateway_coordinator(monkeypatch):
    monkeypatch.setattr(startup, "_running_in_container", lambda: True)
    monkeypatch.setattr(startup, "_s6_service_manager_available", lambda: False)
    seen = []

    result = startup.ensure_all_profile_gateways(
        list_profiles=profiles,
        start_profile=lambda profile: seen.append(profile["name"])
        or {"profile": profile["name"], "status": "started"},
    )

    assert sorted(seen) == ["abc", "default"]
    assert result["counts"] == {"started": 2}


def test_s6_container_keeps_webui_gateway_coordinator_enabled(monkeypatch):
    monkeypatch.setattr(startup, "_running_in_container", lambda: True)
    monkeypatch.setattr(startup, "_s6_service_manager_available", lambda: True)
    seen = []

    result = startup.ensure_all_profile_gateways(
        list_profiles=profiles,
        start_profile=lambda profile: seen.append(profile["name"])
        or {"profile": profile["name"], "status": "started"},
    )

    assert sorted(seen) == ["abc", "default"]
    assert result["counts"] == {"started": 2}


def test_explicit_enable_overrides_plain_container_detection(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_START_PROFILE_GATEWAYS", "1")
    monkeypatch.setattr(startup, "_running_in_container", lambda: True)
    monkeypatch.setattr(startup, "_s6_service_manager_available", lambda: False)

    assert startup.gateway_autostart_enabled() is True


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
    from integration.gateway_startup import runtime as agent_cli_runtime

    original_home = os.environ.get("HERMES_HOME")
    verified_env = {"PYTHONUTF8": "1"}
    invocation = agent_cli_runtime.AgentCliInvocation(
        ("/hermes",), str(Path.home()), verified_env, "managed_launcher"
    )
    monkeypatch.setattr(agent_cli_runtime, "resolve_agent_cli_runtime", lambda: invocation)
    captured = {}

    def process_starter(profile, *, runtime):
        captured["profile"] = profile
        captured["runtime"] = runtime
        return {"profile": profile["name"], "status": "started", "pid": 42}

    result = startup._start_profile_gateway(profiles()[0], process_starter=process_starter)

    assert result["status"] == "started"
    assert captured["profile"] == profiles()[0]
    assert captured["runtime"] is invocation
    assert os.environ.get("HERMES_HOME") == original_home


def test_s6_keeps_service_manager_lifecycle(monkeypatch):
    from integration.gateway_startup import runtime as agent_cli_runtime

    invocation = agent_cli_runtime.AgentCliInvocation(("/hermes",), str(Path.home()), {"X": "1"}, "launcher")
    monkeypatch.setattr(startup, "_running_in_container", lambda: True)
    monkeypatch.setattr(startup, "_s6_service_manager_available", lambda: True)
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    result = startup._start_profile_gateway(
        profiles()[0], runtime_resolver=lambda: invocation, service_runner=runner
    )

    assert result == {"profile": "default", "status": "started", "owner": "s6"}
    assert captured["command"] == ["/hermes", "gateway", "start"]
    assert captured["cwd"] == str(Path.home())
    assert captured["env"] == {"X": "1"}


def test_runtime_unavailable_is_classified(monkeypatch):
    from integration.gateway_startup import runtime as agent_cli_runtime

    def unavailable():
        raise agent_cli_runtime.AgentCliRuntimeUnavailable("No verified runtime")

    monkeypatch.setattr(agent_cli_runtime, "resolve_agent_cli_runtime", unavailable)

    result = startup._start_profile_gateway(profiles()[0])

    assert result["status"] == "runtime_unavailable"
    assert result["profile"] == "default"


def test_runtime_resolution_retries_transient_failure(monkeypatch):
    from integration.gateway_startup import runtime as agent_cli_runtime

    invocation = agent_cli_runtime.AgentCliInvocation(("/hermes",), str(Path.home()), {}, "launcher")
    attempts = []

    def resolver():
        attempts.append(True)
        if len(attempts) < 3:
            raise agent_cli_runtime.AgentCliRuntimeUnavailable("temporary probe failure")
        return invocation

    monkeypatch.setattr(startup.time, "sleep", lambda _seconds: None)
    result = startup._start_profile_gateway(
        profiles()[0],
        runtime_resolver=resolver,
        process_starter=lambda profile, *, runtime: {
            "profile": profile["name"],
            "status": "started",
        },
    )

    assert result == {"profile": "default", "status": "started"}
    assert len(attempts) == 3


def test_ui_gateway_running_flag_is_not_an_authoritative_process_probe(monkeypatch):
    profile = profiles()[0]
    profile["gateway_running"] = True
    from integration.gateway_startup import runtime as agent_cli_runtime

    invocation = agent_cli_runtime.AgentCliInvocation(("/hermes",), str(Path.home()), {}, "launcher")
    seen = []

    result = startup._start_profile_gateway(
        profile,
        runtime_resolver=lambda: invocation,
        process_starter=lambda got_profile, *, runtime: seen.append((got_profile, runtime))
        or {"profile": "default", "status": "state_unknown"},
    )

    assert result == {"profile": "default", "status": "state_unknown"}
    assert seen == [(profile, invocation)]
