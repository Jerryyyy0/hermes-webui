"""Cron-only execution workspace cleanup."""

from __future__ import annotations

from pathlib import Path

def cleanup_execution_workspace(
    root: str | Path,
    *,
    session_id: str,
    mode: str,
    repo_root: str | Path | None = None,
) -> dict[str, object]:
    """Remove an owned managed root or detached worktree, never a guessed path."""
    try:
        from cron.execution_workspace import cleanup_execution_workspace as agent_cleanup

        target = Path(root).expanduser().resolve()
        inferred_base = None
        if len(target.parents) >= 4 and target.parents[1].name == "cron" and target.parents[2].name == "sessions":
            inferred_base = target.parents[3]

        return agent_cleanup(
            root,
            session_id=session_id,
            strategy=mode,
            repo_root=repo_root,
            namespace_base=inferred_base,
        )
    except Exception as exc:
        return {"ok": False, "deleted": False, "reason": str(exc)}
