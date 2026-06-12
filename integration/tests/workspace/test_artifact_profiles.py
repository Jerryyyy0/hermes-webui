import json
from unittest.mock import patch

import pytest

from api.models import Session
from integration.workspace.artifact_profiles import (
    build_workspace_artifact_profile_index,
    get_workspace_artifact_profile_index,
    invalidate_workspace_artifact_profile_index,
)


def _write_session_index(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_build_index_latest_session_wins(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    artifact = workspace / "notes.txt"
    artifact.write_text("hello", encoding="utf-8")

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_DIR", session_dir)
    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.models.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.models.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.config.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.config.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.session_manifest._load_display_messages", lambda s: list(s.messages))

    sid_old = "sessold01abc"
    sid_new = "sessnew01abc"
    Session(
        session_id=sid_old,
        workspace=str(workspace),
        profile="default",
        updated_at=100.0,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "write_file", "arguments": '{"path":"notes.txt"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
    ).save(touch_updated_at=False, skip_index=True)
    Session(
        session_id=sid_new,
        workspace=str(workspace),
        profile="ops",
        updated_at=200.0,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "write_file", "arguments": '{"path":"notes.txt"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        ],
    ).save(touch_updated_at=False, skip_index=True)

    _write_session_index(index_file, [
        {"session_id": sid_old, "message_count": 2, "updated_at": 100.0},
        {"session_id": sid_new, "message_count": 2, "updated_at": 200.0},
    ])

    index, _by_session = build_workspace_artifact_profile_index(workspace_root=workspace)
    assert index == {"notes.txt": "ops"}


def test_get_index_uses_cache_until_mtime_changes(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    _write_session_index(index_file, [])

    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_DIR", session_dir)
    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_INDEX_FILE", index_file)

    invalidate_workspace_artifact_profile_index(workspace)
    with patch(
        "integration.workspace.artifact_profiles._rebuild_path_sources",
        return_value={"a.txt": {"sid1": ("ops", 1.0)}},
    ) as rebuild:
        first = get_workspace_artifact_profile_index(workspace)
        second = get_workspace_artifact_profile_index(workspace)
        assert first == {"a.txt": "ops"}
        assert second == {"a.txt": "ops"}
        assert rebuild.call_count == 1


def test_update_workspace_artifact_profile_for_session(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_DIR", session_dir)
    monkeypatch.setattr("integration.workspace.artifact_profiles.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.models.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.models.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.config.SESSION_DIR", session_dir)
    monkeypatch.setattr("api.config.SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr("api.session_manifest._load_display_messages", lambda s: list(s.messages))

    sid = "sessnew01abc"
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        profile="ops",
        updated_at=200.0,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "write_file", "arguments": '{"path":"notes.txt"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
    )

    invalidate_workspace_artifact_profile_index(workspace)
    get_workspace_artifact_profile_index(workspace)

    from integration.workspace.artifact_profiles import update_workspace_artifact_profile_for_session

    update_workspace_artifact_profile_for_session(session, workspace_root=workspace)
    index = get_workspace_artifact_profile_index(workspace)
    assert index == {"notes.txt": "ops"}
