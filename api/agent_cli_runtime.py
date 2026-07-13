"""Resolve a dependency-complete Hermes CLI runtime."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class AgentCliRuntimeUnavailable(RuntimeError):
    """Raised when no verified Hermes CLI runtime can be found."""


@dataclass(frozen=True)
class AgentCliInvocation:
    command_prefix: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    kind: str

    def command(self, *args: str) -> list[str]:
        return [*self.command_prefix, *args]


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["BROWSER"] = "echo"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _agent_dir() -> Path | None:
    try:
        from api.config import _AGENT_DIR
    except Exception:
        return None
    return Path(_AGENT_DIR) if _AGENT_DIR is not None else None


def _is_executable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _runtime_candidates(agent_dir: Path | None) -> list[tuple[str, tuple[str, ...], bool]]:
    candidates: list[tuple[str, tuple[str, ...], bool]] = []
    explicit = os.environ.get("HERMES_WEBUI_HERMES_EXECUTABLE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise AgentCliRuntimeUnavailable("HERMES_WEBUI_HERMES_EXECUTABLE must be an absolute path")
        candidates.append(("explicit_launcher", (str(path),), False))

    if agent_dir is not None:
        layouts = (
            (agent_dir / "venv" / "bin" / "hermes", agent_dir / "venv" / "bin" / "python"),
            (agent_dir / ".venv" / "bin" / "hermes", agent_dir / ".venv" / "bin" / "python"),
            (agent_dir / "venv" / "Scripts" / "hermes.exe", agent_dir / "venv" / "Scripts" / "python.exe"),
            (agent_dir / ".venv" / "Scripts" / "hermes.exe", agent_dir / ".venv" / "Scripts" / "python.exe"),
        )
        for launcher, _python in layouts:
            candidates.append(("managed_launcher", (str(launcher),), False))
        for _launcher, python in layouts:
            candidates.append(("managed_python", (str(python), "-m", "hermes_cli.main"), True))

    path_launcher = shutil.which("hermes")
    if path_launcher:
        candidates.append(("path_launcher", (path_launcher,), False))

    seen = set()
    unique = []
    for candidate in candidates:
        key = candidate[1]
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _probe_candidate(
    prefix: tuple[str, ...],
    *,
    is_python: bool,
    runner: Callable = subprocess.run,
) -> bool:
    executable = Path(prefix[0])
    if not _is_executable(executable):
        return False
    if is_python:
        command = [
            prefix[0],
            "-c",
            "import hermes_cli.main, rich, yaml",
        ]
    else:
        command = [prefix[0], "--version"]
    try:
        completed = runner(
            command,
            cwd=str(Path.home()),
            env=_base_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def resolve_agent_cli_runtime(
    *,
    agent_dir: Path | None = None,
    probe_runner: Callable = subprocess.run,
) -> AgentCliInvocation:
    """Return the first verified Hermes CLI runtime, or fail closed."""
    resolved_agent_dir = agent_dir if agent_dir is not None else _agent_dir()
    attempted = []
    for kind, prefix, is_python in _runtime_candidates(resolved_agent_dir):
        attempted.append(kind)
        if _probe_candidate(prefix, is_python=is_python, runner=probe_runner):
            return AgentCliInvocation(
                command_prefix=prefix,
                cwd=str(Path.home()),
                env=_base_env(),
                kind=kind,
            )
    summary = ", ".join(dict.fromkeys(attempted)) or "none"
    raise AgentCliRuntimeUnavailable(f"No verified Hermes CLI runtime (attempted: {summary})")


def build_gateway_command(runtime: AgentCliInvocation, profile: dict, action: str) -> list[str]:
    name = str(profile.get("name") or "").strip()
    args = []
    if not profile.get("is_default"):
        if not name:
            raise ValueError("Profile name is missing")
        args.extend(["-p", name])
    args.extend(["gateway", action])
    return runtime.command(*args)
