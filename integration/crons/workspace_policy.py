"""Validation and binding for Cron V1 execution workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CronWorkspacePolicyError(ValueError):
    """A Cron workspace policy or execution binding cannot be trusted."""


def approved_default_workspace(profile_home: str | Path | None = None) -> Path:
    """Resolve the approved default used by the selected WebUI profile."""
    from api.config import DEFAULT_WORKSPACE, get_config_for_profile_home
    from api.workspace import resolve_trusted_workspace

    home = Path(profile_home).expanduser() if profile_home else None
    candidates: list[str] = []
    if home is not None:
        try:
            value = (home / "webui_state" / "last_workspace.txt").read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            candidates.append(value)
    config = get_config_for_profile_home(home)
    for key in ("workspace", "default_workspace"):
        value = config.get(key) if isinstance(config, dict) else None
        if value:
            candidates.append(str(value))
    terminal = config.get("terminal") if isinstance(config, dict) else None
    if isinstance(terminal, dict) and terminal.get("cwd") not in (None, "", "."):
        candidates.append(str(terminal["cwd"]))
    candidates.append(str(DEFAULT_WORKSPACE))
    for value in candidates:
        try:
            return resolve_trusted_workspace(value)
        except (OSError, RuntimeError, ValueError):
            continue
    raise CronWorkspacePolicyError("当前 Profile 没有可用的默认 workspace")


@dataclass(frozen=True)
class CronWorkspaceBinding:
    root: str
    mode: str
    state: str
    strategy: str
    worktree_repo_root: str | None = None


def normalize_workspace_policy(value: Any, *, default_base: str | Path | None = None) -> dict[str, object]:
    """Return a complete, trusted V1 policy for Cron Hub persistence."""
    if value is None:
        value = {
            "version": 1,
            "strategy": "managed",
            "base_workspace": str(default_base or approved_default_workspace()),
        }
    if not isinstance(value, dict):
        raise CronWorkspacePolicyError("workspace_policy 必须是对象")
    if value.get("version") != 1:
        raise CronWorkspacePolicyError("workspace_policy.version 必须为 1")
    strategy = str(value.get("strategy") or "").strip().lower()
    if strategy not in {"managed", "worktree"}:
        raise CronWorkspacePolicyError("workspace_policy.strategy 必须为 managed 或 worktree")
    raw_base = str(value.get("base_workspace") or "").strip()
    if not raw_base:
        raise CronWorkspacePolicyError("workspace_policy.base_workspace 不能为空")
    from api.workspace import resolve_trusted_workspace

    try:
        base = resolve_trusted_workspace(raw_base)
    except (TypeError, ValueError) as exc:
        raise CronWorkspacePolicyError(str(exc)) from exc
    if strategy == "worktree":
        import subprocess

        probe = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode:
            raise CronWorkspacePolicyError("worktree 的 base_workspace 必须是 Git 仓库")
    return {"version": 1, "strategy": strategy, "base_workspace": str(base)}


def binding_for_current_run(
    job: dict[str, Any],
    *,
    session_id: str | None,
    cwd: str | Path | None,
    state_db_cwd: str | Path | None,
) -> CronWorkspaceBinding:
    """Validate the explicit V1 values propagated from the current run."""
    policy = (job or {}).get("workspace_policy")
    if not isinstance(policy, dict):
        raise CronWorkspacePolicyError("当前运行没有 V1 workspace policy")
    strategy = str(policy.get("strategy") or "").strip().lower()
    if policy.get("version") != 1 or strategy not in {"managed", "worktree"}:
        raise CronWorkspacePolicyError("当前运行的 workspace policy 无效")
    sid = str(session_id or "").strip()
    if not sid or sid != str((job or {}).get("_cron_session_id") or "").strip():
        raise CronWorkspacePolicyError("当前运行的 session ID 不一致")
    raw_cwd = str(cwd or "").strip()
    raw_state_cwd = str(state_db_cwd or "").strip()
    if not raw_cwd or not raw_state_cwd:
        raise CronWorkspacePolicyError("当前运行缺少 canonical workspace")
    root = Path(raw_cwd).expanduser().resolve()
    state_root = Path(raw_state_cwd).expanduser().resolve()
    if root != state_root or not root.is_dir():
        raise CronWorkspacePolicyError("当前运行 workspace 与 state.db 不一致")
    from api.workspace import resolve_trusted_workspace

    trusted_root = resolve_trusted_workspace(root)
    if strategy == "managed":
        base = Path(str(policy.get("base_workspace") or "")).expanduser().resolve()
        try:
            trusted_root.relative_to(base / "sessions" / "cron")
        except ValueError as exc:
            raise CronWorkspacePolicyError("managed workspace 不在 Cron 管理目录内") from exc
    return CronWorkspaceBinding(
        root=str(trusted_root),
        mode=strategy,
        state="ready",
        strategy=strategy,
        worktree_repo_root=(
            str((job or {}).get("_cron_worktree_repo_root") or "").strip() or None
        ),
    )


def binding_for_legacy_session(
    job: dict[str, Any],
    *,
    state_db_cwd: str | Path | None,
    profile_home: str | Path | None,
) -> CronWorkspaceBinding:
    """Bind a legacy Cron session to a trusted shared continuation workspace.

    Legacy jobs have no per-execution workspace contract, so this value never
    claims to be the original execution cwd.  It is only the stable directory
    selected for a user's later WebUI follow-up.
    """
    from api.workspace import resolve_trusted_workspace

    for value in (state_db_cwd, (job or {}).get("workdir")):
        if value in (None, ""):
            continue
        try:
            root = resolve_trusted_workspace(value)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        return CronWorkspaceBinding(
            root=str(root),
            mode="external",
            state="legacy_shared",
            strategy="legacy",
        )

    root = approved_default_workspace(profile_home)
    return CronWorkspaceBinding(
        root=str(root),
        mode="external",
        state="legacy_shared",
        strategy="legacy",
    )
