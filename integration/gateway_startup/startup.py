"""Best-effort startup of Hermes gateways for every visible profile."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_WORKERS = 4
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_state_lock = threading.Lock()
_started = False
_running = False


def gateway_autostart_enabled() -> bool:
    raw = os.environ.get("HERMES_WEBUI_START_PROFILE_GATEWAYS", "").strip().lower()
    if not raw:
        return True
    if raw in _FALSE_VALUES:
        return False
    return raw in _TRUE_VALUES


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


def _start_profile_gateway(
    profile: dict,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    runner: Callable = subprocess.run,
) -> dict:
    name = str(profile.get("name") or "").strip() or "default"
    if profile.get("gateway_running") is True:
        return {"profile": name, "status": "already_running"}

    try:
        from api.agent_cli_runtime import (
            AgentCliRuntimeUnavailable,
            build_gateway_command,
            resolve_agent_cli_runtime,
        )

        try:
            runtime = resolve_agent_cli_runtime()
        except AgentCliRuntimeUnavailable as exc:
            return {"profile": name, "status": "runtime_unavailable", "error": str(exc)}
        completed = runner(
            build_gateway_command(runtime, profile, "start"),
            cwd=runtime.cwd,
            env=runtime.env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"profile": name, "status": "timed_out"}
    except Exception as exc:
        return {"profile": name, "status": "failed", "error": type(exc).__name__}

    if completed.returncode == 0:
        return {"profile": name, "status": "started"}
    return {
        "profile": name,
        "status": "failed",
        "error": f"gateway start exited with status {completed.returncode}",
    }


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
    """Ensure profile gateways are running once per WebUI process."""
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

        starter = start_profile or _start_profile_gateway
        results = []
        if profiles:
            workers = max(1, min(int(max_workers), len(profiles)))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="profile-gateway-start",
            ) as pool:
                futures = [pool.submit(starter, profile) for profile in profiles]
                for future in futures:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"profile": "unknown", "status": "failed", "error": type(exc).__name__})
        results.extend(skipped)
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "failed")
            counts[status] = counts.get(status, 0) + 1
        logger.info("Profile gateway startup completed: %s", counts)
        for result in results:
            if result.get("status") in {"failed", "timed_out", "runtime_unavailable"}:
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
