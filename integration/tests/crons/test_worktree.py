"""Lifecycle tests for Cron-owned execution roots."""

def test_cleanup_removes_only_verified_managed_execution_root(tmp_path):
    from integration.crons.worktree import cleanup_execution_workspace

    sid = "cron_job1_20260819_010101"
    root = tmp_path / "sessions" / "cron" / "default" / sid
    root.mkdir(parents=True)
    (root / "artifact.txt").write_text("owned", encoding="utf-8")

    result = cleanup_execution_workspace(root, session_id=sid, mode="managed")

    assert result == {"ok": True, "deleted": True, "mode": "managed"}
    assert not root.exists()


def test_cleanup_rejects_root_outside_cron_namespace(tmp_path):
    from integration.crons.worktree import cleanup_execution_workspace

    sid = "cron_job1_20260819_010101"
    root = tmp_path / "not-cron" / sid
    root.mkdir(parents=True)

    result = cleanup_execution_workspace(root, session_id=sid, mode="managed")

    assert result["ok"] is False
    assert result["reason"] == "unverified_workspace_root"
    assert root.exists()
