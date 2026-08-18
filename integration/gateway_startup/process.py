"""Lifecycle owner for Gateway processes spawned by this WebUI instance."""

from __future__ import annotations

import codecs
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal

from integration.gateway_startup.redaction import redact_gateway_console_line
from integration.gateway_startup.runtime import (
    AgentCliInvocation,
    build_agent_python_command,
    build_gateway_run_command,
)
from integration.project_logging import log_gateway_line
from integration.project_logging.formatting import one_line

logger = logging.getLogger(__name__)

GatewayState = Literal["running", "not_running", "unknown"]

_READ_CHUNK_BYTES = 64 * 1024
_MAX_PENDING_LINE_BYTES = 64 * 1024
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b")
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_PLANNED_RESTART_EXIT_CODE = 75


@dataclass
class GatewayProcess:
    profile: dict
    runtime: AgentCliInvocation
    process: subprocess.Popen
    reader: threading.Thread | None = None
    wait_thread: threading.Thread | None = None
    stopping: threading.Event = field(default_factory=threading.Event)


_registry_lock = threading.RLock()
_registry: dict[str, GatewayProcess] = {}
_manager_stopping = threading.Event()


def _profile_key(profile: dict) -> str:
    path = str(profile.get("path") or "").strip()
    name = str(profile.get("name") or "").strip() or "default"
    return path or name


def _profile_name(profile: dict) -> str:
    return str(profile.get("name") or "").strip() or "default"


_STATE_PROBE_SCRIPT = (
    "import sys; from pathlib import Path; "
    "from gateway.status import get_running_pid; "
    "pid = get_running_pid(Path(sys.argv[1]) / 'gateway.pid', cleanup_stale=False); "
    "print('running' if pid else 'not_running')"
)


def probe_profile_gateway_state(profile: dict, runtime: AgentCliInvocation) -> GatewayState:
    """Read the Agent's profile-scoped lock through its authoritative helper."""
    path = str(profile.get("path") or "").strip()
    if not path:
        return "unknown"
    command = build_agent_python_command(runtime, "-c", _STATE_PROBE_SCRIPT, path)
    if command is None:
        return "unknown"
    try:
        completed = subprocess.run(
            command,
            cwd=runtime.cwd,
            env=runtime.env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            return "unknown"
        status = completed.stdout.strip()
        return status if status in {"running", "not_running"} else "unknown"
    except Exception:
        return "unknown"


def _sanitize_line(line: str) -> str:
    line = _ANSI_RE.sub("", line)
    line = "".join(ch for ch in line if ch == "\t" or ord(ch) >= 32)
    return one_line(redact_gateway_console_line(line), max_len=_MAX_PENDING_LINE_BYTES)


def _emit_line(profile: dict, line: str, **flags: bool) -> None:
    clean = _sanitize_line(line)
    if clean == "-":
        return
    suffix = "".join(f" {key}=true" for key, value in flags.items() if value)
    match = _LEVEL_RE.search(clean)
    level = _LEVELS.get(match.group(1), logging.INFO) if match else logging.INFO
    log_gateway_line(level, f"[gateway:{_profile_name(profile)}] {clean}{suffix}")


def _reader_loop(entry: GatewayProcess) -> None:
    pipe = entry.process.stdout
    if pipe is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    discarding = False
    try:
        while True:
            chunk = os.read(pipe.fileno(), _READ_CHUNK_BYTES)
            if not chunk:
                break
            text = decoder.decode(chunk)
            while text:
                if discarding:
                    newline = text.find("\n")
                    if newline < 0:
                        text = ""
                        continue
                    discarding = False
                    text = text[newline + 1 :]
                    continue
                newline = text.find("\n")
                if newline >= 0:
                    pending += text[:newline]
                    _emit_line(entry.profile, pending)
                    pending = ""
                    text = text[newline + 1 :]
                    continue
                pending += text
                text = ""
                if len(pending.encode("utf-8", errors="replace")) >= _MAX_PENDING_LINE_BYTES:
                    _emit_line(entry.profile, pending, truncated=True)
                    pending = ""
                    discarding = True
        pending += decoder.decode(b"", final=True)
        if pending:
            _emit_line(entry.profile, pending, unterminated=True)
    except Exception:
        logger.warning("Gateway log reader failed for %s", _profile_name(entry.profile), exc_info=True)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _wait_for_exit(entry: GatewayProcess) -> None:
    returncode = entry.process.wait()
    if entry.reader is not None:
        entry.reader.join(timeout=2)
    name = _profile_name(entry.profile)
    expected = entry.stopping.is_set() or _manager_stopping.is_set()
    planned = not expected and returncode == _PLANNED_RESTART_EXIT_CODE
    if expected:
        logger.info("WebUI-owned Gateway exited during shutdown: %s (pid=%s, code=%s)", name, entry.process.pid, returncode)
    elif planned:
        logger.info("WebUI-owned Gateway requested planned restart: %s (pid=%s)", name, entry.process.pid)
    elif returncode:
        logger.warning("WebUI-owned Gateway exited: %s (pid=%s, code=%s)", name, entry.process.pid, returncode)
    else:
        logger.info("WebUI-owned Gateway exited: %s (pid=%s, code=0)", name, entry.process.pid)

    key = _profile_key(entry.profile)
    with _registry_lock:
        if _registry.get(key) is entry:
            _registry.pop(key, None)
    if planned:
        _restart_planned_gateway(entry)


def _restart_planned_gateway(previous: GatewayProcess) -> None:
    if _manager_stopping.is_set():
        return
    if probe_profile_gateway_state(previous.profile, previous.runtime) != "not_running":
        logger.warning("Gateway planned restart not relaunched for %s: status is not not_running", _profile_name(previous.profile))
        return
    result = start_gateway_process(
        previous.profile,
        runtime=previous.runtime,
        state_probe=lambda _profile, _runtime: "not_running",
    )
    if result.get("status") != "started":
        logger.warning("Gateway planned restart failed for %s", _profile_name(previous.profile))


def _popen_kwargs() -> dict:
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "bufsize": 0}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def start_gateway_process(
    profile: dict,
    *,
    runtime: AgentCliInvocation,
    popen_factory: Callable = subprocess.Popen,
    state_probe: Callable[[dict, AgentCliInvocation], GatewayState] = probe_profile_gateway_state,
) -> dict:
    """Start one foreground Gateway and connect its output to WebUI logging."""
    key = _profile_key(profile)
    name = _profile_name(profile)
    if not key:
        return {"profile": name, "status": "failed", "error": "profile_missing"}
    with _registry_lock:
        if key in _registry:
            return {"profile": name, "status": "already_owned"}
    state = state_probe(profile, runtime)
    if state == "running":
        return {"profile": name, "status": "already_running"}
    if state != "not_running":
        return {"profile": name, "status": "state_unknown"}
    try:
        process = popen_factory(
            build_gateway_run_command(runtime, profile),
            cwd=runtime.cwd,
            env=runtime.env,
            **_popen_kwargs(),
        )
    except OSError as exc:
        return {"profile": name, "status": "failed", "error": type(exc).__name__}

    placeholder = GatewayProcess(profile=profile, runtime=runtime, process=process)
    placeholder.reader = threading.Thread(target=_reader_loop, args=(placeholder,), name=f"gateway-log-{name}", daemon=True)
    placeholder.wait_thread = threading.Thread(target=_wait_for_exit, args=(placeholder,), name=f"gateway-exit-{name}", daemon=True)
    with _registry_lock:
        if _manager_stopping.is_set() or key in _registry:
            placeholder.stopping.set()
            process.terminate()
            return {"profile": name, "status": "not_started"}
        _registry[key] = placeholder
    placeholder.reader.start()
    placeholder.wait_thread.start()
    return {"profile": name, "status": "started", "pid": process.pid}


def _terminate(entry: GatewayProcess) -> None:
    entry.stopping.set()
    if entry.process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(entry.process.pid, signal.SIGTERM)
        else:
            entry.process.terminate()
    except (OSError, ProcessLookupError):
        pass


def stop_gateway_processes(timeout: float = 8.0) -> None:
    """Stop only child process groups owned by this WebUI process."""
    _manager_stopping.set()
    with _registry_lock:
        entries = list(_registry.values())
    for entry in entries:
        _terminate(entry)
    for entry in entries:
        if entry.wait_thread is not None:
            entry.wait_thread.join(timeout=timeout)
        if entry.process.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(entry.process.pid, signal.SIGKILL)
                else:
                    entry.process.kill()
            except (OSError, ProcessLookupError):
                pass
    with _registry_lock:
        _registry.clear()


def _reset_process_manager_for_tests() -> None:
    _manager_stopping.clear()
    with _registry_lock:
        _registry.clear()
