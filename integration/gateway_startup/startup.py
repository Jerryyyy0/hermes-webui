"""Best-effort startup of Hermes gateways for every visible profile."""

from __future__ import annotations

import importlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from integration.project_logging import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_WORKERS = 4
_EXTERNAL_SERVICE_RESTART_TIMEOUT_SECONDS = 300.0
_RUNTIME_RESOLUTION_ATTEMPTS = 3
_RUNTIME_RESOLUTION_RETRY_SECONDS = 1.0
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_state_lock = threading.Lock()
_started = False
_running = False


def _running_in_container() -> bool:
    if os.environ.get("container", "").strip():
        return True
    return Path("/.within_container").is_file() or Path("/.dockerenv").is_file()


def _s6_service_manager_available() -> bool:
    try:
        service_manager = importlib.import_module("hermes_cli.service_manager")
        return service_manager.detect_service_manager() == "s6"
    except Exception:
        return False


def gateway_autostart_enabled() -> bool:
    raw = os.environ.get("HERMES_WEBUI_START_PROFILE_GATEWAYS", "").strip().lower()
    if raw:
        if raw in _FALSE_VALUES:
            return False
        return raw in _TRUE_VALUES
    return True


def _multiplex_enabled(default_home: Path) -> bool:
    from api.config import get_config_for_profile_home

    config = get_config_for_profile_home(default_home)
    if not isinstance(config, dict):
        return False
    gateway = config.get("gateway")
    nested = gateway.get("multiplex_profiles") if isinstance(gateway, dict) else None
    value = config.get("multiplex_profiles", nested)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return bool(value)


def _resolve_runtime_with_retries(runtime_resolver: Callable) -> tuple[object | None, Exception | None]:
    """Resolve one verified Agent runtime without amplifying startup probes."""
    from integration.gateway_startup.runtime import AgentCliRuntimeUnavailable

    runtime = None
    runtime_error = None
    for attempt in range(_RUNTIME_RESOLUTION_ATTEMPTS):
        try:
            runtime = runtime_resolver()
            break
        except AgentCliRuntimeUnavailable as exc:
            runtime_error = exc
            if attempt + 1 < _RUNTIME_RESOLUTION_ATTEMPTS:
                time.sleep(_RUNTIME_RESOLUTION_RETRY_SECONDS)
    return runtime, runtime_error


def _start_profile_gateway(
    profile: dict,
    *,
    runtime: object | None = None,
    runtime_resolver: Callable | None = None,
    process_starter: Callable | None = None,
    service_runner: Callable = subprocess.run,
    state_probe: Callable | None = None,
    owner_probe: Callable | None = None,
) -> dict:
    """Start a Gateway under the correct lifecycle owner.

    Native hosts restart a running service-managed Gateway through its Agent
    service manager, then follow its files into the WebUI console. Other
    running Gateway processes are replaced by a WebUI-owned foreground child.
    Plain containers use the same WebUI-owned path, while s6 remains owned by
    its service manager.
    """
    name = str(profile.get("name") or "").strip() or "default"
    try:
        from integration.gateway_startup.runtime import (
            bind_runtime_to_profile,
            build_gateway_command,
            resolve_agent_cli_runtime,
        )
        from integration.gateway_startup.process import (
            follow_external_gateway_logs,
            probe_profile_gateway_owner,
            probe_profile_gateway_state,
            start_gateway_process,
        )
        resolver = runtime_resolver or resolve_agent_cli_runtime
        runtime_error = None
        if runtime is None:
            runtime, runtime_error = _resolve_runtime_with_retries(resolver)
        if runtime is None:
            return {"profile": name, "status": "runtime_unavailable", "error": str(runtime_error)}
        runtime = bind_runtime_to_profile(runtime, profile)
        in_container = _running_in_container()
        if in_container and _s6_service_manager_available():
            completed = service_runner(
                build_gateway_command(runtime, profile, "restart"),
                cwd=runtime.cwd,
                env=runtime.env,
                capture_output=True,
                text=True,
                timeout=_EXTERNAL_SERVICE_RESTART_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode == 0:
                return {"profile": name, "status": "restarted", "owner": "s6"}
            return {
                "profile": name,
                "status": "failed",
                "error": f"gateway restart exited with status {completed.returncode}",
            }

        starter = process_starter or start_gateway_process
        if not in_container:
            state = (state_probe or probe_profile_gateway_state)(profile, runtime)
            if state == "running":
                owner = (owner_probe or probe_profile_gateway_owner)(profile, runtime)
                if owner == "managed":
                    follow_external_gateway_logs(profile)
                    completed = service_runner(
                        build_gateway_command(runtime, profile, "restart"),
                        cwd=runtime.cwd,
                        env=runtime.env,
                        timeout=_EXTERNAL_SERVICE_RESTART_TIMEOUT_SECONDS,
                        check=False,
                    )
                    if completed.returncode == 0:
                        return {"profile": name, "status": "restarted", "owner": "external"}
                    return {
                        "profile": name,
                        "status": "failed",
                        "error": f"gateway restart exited with status {completed.returncode}",
                    }
                return starter(profile, runtime=runtime, replace_existing=True)
            if state == "unknown":
                return starter(profile, runtime=runtime, replace_existing=True)

        if in_container:
            return starter(profile, runtime=runtime, replace_existing=True)
        return starter(profile, runtime=runtime)
    except subprocess.TimeoutExpired:
        return {"profile": name, "status": "timed_out"}
    except Exception as exc:
        return {"profile": name, "status": "failed", "error": type(exc).__name__}


def _deduplicate_profiles(profiles: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "").strip()
        path = str(profile.get("path") or "").strip()
        key = path or name
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(profile)
    return result


def ensure_all_profile_gateways(
    *,
    list_profiles: Callable[[], list[dict]] | None = None,
    start_profile: Callable[[dict], dict] | None = None,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> dict:
    """Ensure profile gateways are ready once per WebUI process.

    Profile launches intentionally serialize.  The Agent uses process-scoped
    PID and runtime-lock files during startup; parallel ``--replace`` launches
    can temporarily observe another profile's handoff state as their own.
    """
    global _running, _started

    if not gateway_autostart_enabled():
        return {"enabled": False, "results": []}

    with _state_lock:
        if _started or _running:
            return {"enabled": True, "already_invoked": True, "results": []}
        _running = True

    try:
        if list_profiles is None:
            from api.profiles import list_profiles_api

            list_profiles = list_profiles_api
        profiles = _deduplicate_profiles(list_profiles())
        default_profile = next((p for p in profiles if p.get("is_default")), None)
        if default_profile is not None:
            profiles = [default_profile, *(p for p in profiles if p is not default_profile)]
        multiplex = False
        if default_profile is not None:
            multiplex = _multiplex_enabled(Path(str(default_profile.get("path") or "")))

        skipped = []
        if multiplex:
            skipped = [
                {"profile": str(p.get("name") or ""), "status": "skipped_multiplex"}
                for p in profiles
                if p is not default_profile
            ]
            profiles = [default_profile]

        results = []
        if profiles and start_profile is None:
            from integration.gateway_startup.runtime import resolve_agent_cli_runtime

            runtime, runtime_error = _resolve_runtime_with_retries(resolve_agent_cli_runtime)
            if runtime is None:
                results = [
                    {
                        "profile": str(profile.get("name") or "").strip() or "default",
                        "status": "runtime_unavailable",
                        "error": str(runtime_error),
                    }
                    for profile in profiles
                ]
            else:
                for profile in profiles:
                    try:
                        results.append(_start_profile_gateway(profile, runtime=runtime))
                    except Exception as exc:
                        results.append({"profile": "unknown", "status": "failed", "error": type(exc).__name__})
        elif profiles:
            starter = start_profile
            for profile in profiles:
                try:
                    results.append(starter(profile))
                except Exception as exc:
                    results.append({"profile": "unknown", "status": "failed", "error": type(exc).__name__})
        results.extend(skipped)
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "failed")
            counts[status] = counts.get(status, 0) + 1
        logger.info("Profile gateway startup completed: %s", counts)
        for result in results:
            if result.get("status") in {"failed", "runtime_unavailable", "state_unknown"}:
                logger.warning(
                    "Profile gateway startup %s for %s%s",
                    result.get("status"),
                    result.get("profile"),
                    f" ({result.get('error')})" if result.get("error") else "",
                )
        with _state_lock:
            _started = True
        return {"enabled": True, "multiplex": multiplex, "results": results, "counts": counts}
    except Exception as exc:
        logger.warning("Profile gateway startup enumeration failed: %s", type(exc).__name__)
        return {"enabled": True, "error": type(exc).__name__, "results": []}
    finally:
        with _state_lock:
            _running = False


def _reset_startup_state_for_tests() -> None:
    global _running, _started
    with _state_lock:
        _running = False
        _started = False
