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
import time
from dataclasses import dataclass, field
from pathlib import Path
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
GatewayOwnerState = Literal["managed", "unmanaged", "unknown"]

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
_EXTERNAL_LOG_POLL_SECONDS = 0.25
_GATEWAY_READINESS_TIMEOUT_SECONDS = 300.0
_GATEWAY_READINESS_POLL_SECONDS = 0.25
_GATEWAY_HEARTBEAT_MAX_AGE_SECONDS = 30.0


@dataclass
class GatewayProcess:
    profile: dict
    runtime: AgentCliInvocation
    process: subprocess.Popen
    reader: threading.Thread | None = None
    wait_thread: threading.Thread | None = None
    stopping: threading.Event = field(default_factory=threading.Event)


@dataclass
class GatewayLogFollower:
    profile: dict
    offsets: dict[Path, int]
    identities: dict[Path, tuple[int, int] | None]
    pending: dict[Path, str] = field(default_factory=dict)
    thread: threading.Thread | None = None
    stopping: threading.Event = field(default_factory=threading.Event)


_registry_lock = threading.RLock()
_registry: dict[str, GatewayProcess] = {}
_follower_registry: dict[str, GatewayLogFollower] = {}
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
_READINESS_PROBE_SCRIPT = (
    "import json, sys, time; from pathlib import Path; "
    "from gateway.status import get_running_pid; "
    "home = Path(sys.argv[1]); "
    "pid = get_running_pid(home / 'gateway.pid', cleanup_stale=False); "
    "state_path = home / 'gateway_state.json'; "
    "heartbeat_path = home / 'state' / 'gateway.heartbeat'; "
    "payload = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}; "
    "state_pid = payload.get('pid'); "
    "ready = (pid is not None and state_pid is not None and int(state_pid) == pid "
    "and payload.get('gateway_state') == 'running' and heartbeat_path.is_file() "
    "and time.time() - heartbeat_path.stat().st_mtime <= float(sys.argv[2])); "
    "print(pid if ready else '')"
)
_OWNER_PROBE_SCRIPT = (
    "import os, sys; "
    "os.environ['HERMES_HOME'] = sys.argv[1]; "
    "from hermes_cli.gateway import get_gateway_runtime_snapshot; "
    "snapshot = get_gateway_runtime_snapshot(); "
    "print('managed' if snapshot.service_running else 'unmanaged')"
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


def probe_profile_gateway_readiness(entry: GatewayProcess) -> bool:
    """Confirm that the spawned child owns a fresh, running Gateway state."""
    path = str(entry.profile.get("path") or "").strip()
    if not path:
        return False
    command = build_agent_python_command(
        entry.runtime,
        "-c",
        _READINESS_PROBE_SCRIPT,
        path,
        str(_GATEWAY_HEARTBEAT_MAX_AGE_SECONDS),
    )
    if command is None:
        return False
    try:
        completed = subprocess.run(
            command,
            cwd=entry.runtime.cwd,
            env=entry.runtime.env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            return False
        return int(completed.stdout.strip()) == entry.process.pid
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False


def probe_profile_gateway_owner(profile: dict, runtime: AgentCliInvocation) -> GatewayOwnerState:
    """Determine whether the Agent service manager owns this running Gateway."""
    path = str(profile.get("path") or "").strip()
    if not path:
        return "unknown"
    command = build_agent_python_command(runtime, "-c", _OWNER_PROBE_SCRIPT, path)
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
        return status if status in {"managed", "unmanaged"} else "unknown"
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


def _emit_text_lines(profile: dict, text: str, pending: str) -> str:
    """Emit complete lines and retain a bounded unfinished tail."""
    pending += text
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        _emit_line(profile, line)
    if len(pending.encode("utf-8", errors="replace")) >= _MAX_PENDING_LINE_BYTES:
        _emit_line(profile, pending, truncated=True)
        return ""
    return pending


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


def _external_gateway_log_paths(profile: dict) -> tuple[Path, Path]:
    home = Path(str(profile.get("path") or "")).expanduser()
    return home / "logs" / "gateway.log", home / "logs" / "gateway.error.log"


def _log_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _follow_external_logs(entry: GatewayLogFollower) -> None:
    paths = tuple(entry.offsets)
    while not entry.stopping.wait(_EXTERNAL_LOG_POLL_SECONDS):
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            identity = stat.st_dev, stat.st_ino
            offset = entry.offsets[path]
            if entry.identities[path] != identity or stat.st_size < offset:
                entry.identities[path] = identity
                offset = 0
            if stat.st_size <= offset:
                continue
            try:
                with path.open("rb") as log_file:
                    log_file.seek(offset)
                    chunk = log_file.read()
                    entry.offsets[path] = log_file.tell()
            except OSError:
                continue
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                entry.pending[path] = _emit_text_lines(
                    entry.profile,
                    text,
                    entry.pending.get(path, ""),
                )


def follow_external_gateway_logs(profile: dict) -> bool:
    """Forward new lines from an externally owned Gateway without restarting it."""
    key = _profile_key(profile)
    if not key or _manager_stopping.is_set():
        return False
    paths = _external_gateway_log_paths(profile)
    offsets = {}
    identities = {}
    for path in paths:
        try:
            offsets[path] = path.stat().st_size
        except OSError:
            offsets[path] = 0
        identities[path] = _log_identity(path)
    with _registry_lock:
        if key in _registry or key in _follower_registry:
            return False
        entry = GatewayLogFollower(profile=profile, offsets=offsets, identities=identities)
        entry.thread = threading.Thread(
            target=_follow_external_logs,
            args=(entry,),
            name=f"gateway-log-follow-{_profile_name(profile)}",
            daemon=True,
        )
        _follower_registry[key] = entry
    entry.thread.start()
    log_gateway_line(
        logging.INFO,
        f"[gateway:{_profile_name(profile)}] INFO WebUI attached to externally managed Gateway logs",
    )
    return True


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


def _wait_for_gateway_readiness(
    entry: GatewayProcess,
    readiness_probe: Callable[[GatewayProcess], bool],
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        returncode = entry.process.poll()
        if returncode is not None:
            return f"exited_{returncode}"
        try:
            if readiness_probe(entry) and entry.process.poll() is None:
                return None
        except Exception:
            logger.debug("Gateway readiness probe failed for %s", _profile_name(entry.profile), exc_info=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "readiness_timeout"
        time.sleep(min(max(0.01, poll_seconds), remaining))


def _stop_unready_gateway(entry: GatewayProcess) -> None:
    entry.stopping.set()
    _terminate(entry)
    try:
        entry.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            entry.process.kill()
        except OSError:
            pass
    if entry.wait_thread is not None:
        entry.wait_thread.join(timeout=2)
    key = _profile_key(entry.profile)
    with _registry_lock:
        if _registry.get(key) is entry:
            _registry.pop(key, None)


def start_gateway_process(
    profile: dict,
    *,
    runtime: AgentCliInvocation,
    popen_factory: Callable = subprocess.Popen,
    state_probe: Callable[[dict, AgentCliInvocation], GatewayState] = probe_profile_gateway_state,
    readiness_probe: Callable[[GatewayProcess], bool] = probe_profile_gateway_readiness,
    replace_existing: bool = False,
    readiness_timeout_seconds: float = _GATEWAY_READINESS_TIMEOUT_SECONDS,
    readiness_poll_seconds: float = _GATEWAY_READINESS_POLL_SECONDS,
) -> dict:
    """Start one foreground Gateway and return only after it is ready."""
    key = _profile_key(profile)
    name = _profile_name(profile)
    if not key:
        return {"profile": name, "status": "failed", "error": "profile_missing"}
    with _registry_lock:
        if key in _registry:
            return {"profile": name, "status": "already_owned"}
    state = state_probe(profile, runtime)
    if state == "running":
        if not replace_existing:
            follow_external_gateway_logs(profile)
            return {"profile": name, "status": "already_running"}
    elif state != "not_running" and not replace_existing:
        return {"profile": name, "status": "state_unknown"}
    try:
        if replace_existing:
            command = build_gateway_run_command(runtime, profile, replace_existing=True)
        else:
            command = build_gateway_run_command(runtime, profile)
        process = popen_factory(
            command,
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
    readiness_error = _wait_for_gateway_readiness(
        placeholder,
        readiness_probe,
        timeout_seconds=readiness_timeout_seconds,
        poll_seconds=readiness_poll_seconds,
    )
    if readiness_error is not None:
        _stop_unready_gateway(placeholder)
        return {"profile": name, "status": "failed", "error": readiness_error}
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
    """Stop owned child processes and detach external-Gateway log followers."""
    _manager_stopping.set()
    with _registry_lock:
        entries = list(_registry.values())
        followers = list(_follower_registry.values())
    for follower in followers:
        follower.stopping.set()
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
        _follower_registry.clear()
    for follower in followers:
        if follower.thread is not None:
            follower.thread.join(timeout=timeout)


def _reset_process_manager_for_tests() -> None:
    _manager_stopping.clear()
    with _registry_lock:
        _registry.clear()
        _follower_registry.clear()
