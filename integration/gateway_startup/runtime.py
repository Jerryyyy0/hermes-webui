"""Resolve a dependency-complete Hermes CLI runtime for Gateway lifecycle calls."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


@dataclass(frozen=True)
class _RuntimeCandidate:
    kind: str
    command_prefix: tuple[str, ...]
    cwd: str
    probe_mode: str


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["BROWSER"] = "echo"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _looks_like_agent_root(path: Path) -> bool:
    return (path / "run_agent.py").is_file() or (path / "hermes_cli" / "main.py").is_file()


def _agent_dir() -> Path | None:
    explicit = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_absolute() and _looks_like_agent_root(path):
            return path.resolve()

    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidate = Path(hermes_home).expanduser() / "hermes-agent"
        if candidate.is_absolute() and _looks_like_agent_root(candidate):
            return candidate.resolve()

    try:
        from api.config import _AGENT_DIR
    except Exception:
        return None
    return Path(_AGENT_DIR) if _AGENT_DIR is not None else None


def _is_executable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _runtime_candidates(agent_dir: Path | None) -> list[_RuntimeCandidate]:
    home = str(Path.home())
    candidates: list[_RuntimeCandidate] = []
    explicit = os.environ.get("HERMES_WEBUI_HERMES_EXECUTABLE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise AgentCliRuntimeUnavailable("HERMES_WEBUI_HERMES_EXECUTABLE must be an absolute path")
        candidates.append(_RuntimeCandidate("explicit_launcher", (str(path),), home, "launcher"))

    if agent_dir is not None:
        layouts = (
            (agent_dir / "venv" / "bin" / "hermes", agent_dir / "venv" / "bin" / "python"),
            (agent_dir / ".venv" / "bin" / "hermes", agent_dir / ".venv" / "bin" / "python"),
            (agent_dir / "venv" / "Scripts" / "hermes.exe", agent_dir / "venv" / "Scripts" / "python.exe"),
            (agent_dir / ".venv" / "Scripts" / "hermes.exe", agent_dir / ".venv" / "Scripts" / "python.exe"),
        )
        for launcher, _python in layouts:
            candidates.append(_RuntimeCandidate("managed_launcher", (str(launcher),), home, "launcher"))
        for _launcher, python in layouts:
            candidates.append(
                _RuntimeCandidate(
                    "managed_python",
                    (str(python), "-m", "hermes_cli.main"),
                    home,
                    "python_module",
                )
            )
        candidates.append(
            _RuntimeCandidate(
                "agent_source_python",
                (sys.executable, "-m", "hermes_cli.main"),
                str(agent_dir),
                "python_module",
            )
        )

    webui_root = Path(__file__).resolve().parents[2]
    candidates.append(
        _RuntimeCandidate(
            "source_python",
            (sys.executable, "-m", "hermes_cli.main"),
            str(webui_root),
            "python_module",
        )
    )

    path_launcher = shutil.which("hermes")
    if path_launcher:
        candidates.append(_RuntimeCandidate("path_launcher", (path_launcher,), home, "launcher"))

    seen = set()
    unique = []
    for candidate in candidates:
        key = (candidate.command_prefix, candidate.cwd)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _probe_candidate(
    candidate: _RuntimeCandidate,
    *,
    runner: Callable = subprocess.run,
) -> bool:
    executable = Path(candidate.command_prefix[0])
    if not _is_executable(executable):
        return False

    commands = []
    if candidate.probe_mode == "python_module":
        commands.append(
            [
                candidate.command_prefix[0],
                "-c",
                "import hermes_cli.main, rich, yaml",
            ]
        )
    commands.append([*candidate.command_prefix, "--version"])

    try:
        for command in commands:
            completed = runner(
                command,
                cwd=candidate.cwd,
                env=_base_env(),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def resolve_agent_cli_runtime(
    *,
    agent_dir: Path | None = None,
    probe_runner: Callable = subprocess.run,
) -> AgentCliInvocation:
    """Return the first verified Hermes CLI runtime, or fail closed."""
    resolved_agent_dir = agent_dir if agent_dir is not None else _agent_dir()
    attempted = []
    for candidate in _runtime_candidates(resolved_agent_dir):
        attempted.append(candidate.kind)
        if _probe_candidate(candidate, runner=probe_runner):
            return AgentCliInvocation(
                command_prefix=candidate.command_prefix,
                cwd=candidate.cwd,
                env=_base_env(),
                kind=candidate.kind,
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


def build_gateway_run_command(runtime: AgentCliInvocation, profile: dict) -> list[str]:
    """Build the fixed foreground command for a WebUI-owned Gateway.

    ``--external-supervisor`` keeps Agent-planned restarts under WebUI process
    management.  Deliberately do not add ``--force`` or ``--replace``: both
    bypass the Agent's normal duplicate-instance protection.
    """
    return [*build_gateway_command(runtime, profile, "run"), "-v", "--external-supervisor"]


def build_agent_python_command(runtime: AgentCliInvocation, *args: str) -> list[str] | None:
    """Build a trusted Agent-Python invocation for a machine-readable probe.

    The WebUI interpreter may not have the Agent package on ``sys.path``.  A
    verified ``*_python`` runtime can run the probe directly; a managed
    launcher has a sibling virtualenv Python.  Arbitrary explicit/PATH
    launchers are intentionally not guessed and therefore fail closed.
    """
    if runtime.kind.endswith("_python"):
        return [runtime.command_prefix[0], *args]
    if runtime.kind == "managed_launcher":
        launcher = Path(runtime.command_prefix[0])
        python_name = "python.exe" if os.name == "nt" else "python"
        candidate = launcher.with_name(python_name)
        if _is_executable(candidate):
            return [str(candidate), *args]
    return None
