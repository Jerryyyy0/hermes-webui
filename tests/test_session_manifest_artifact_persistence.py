"""Regression coverage for durable per-turn artifact decisions."""

from __future__ import annotations

import json
import logging

from api.models import Session


def _manual_cron_session(workspace, *, session_id: str = "cron_manual_artifact01") -> Session:
    artifact = workspace / "gpt-5.6-latest-summary.md"
    artifact.write_text("# summary\n", encoding="utf-8")
    return Session(
        session_id=session_id,
        source_tag="cron",
        workspace=str(workspace),
        profile="abc",
        messages=[
            {"role": "user", "content": "cron request", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "cron response"},
            {"role": "assistant", "content": "context row 1"},
            {"role": "assistant", "content": "context row 2"},
            {"role": "assistant", "content": "context row 3"},
            {"role": "assistant", "content": "context row 4"},
            {"role": "assistant", "content": "context row 5"},
            {"role": "assistant", "content": "context row 6"},
            {"role": "user", "content": "帮我生成一个md", "_turn_key": "turn:2"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "write-turn2",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": str(artifact)}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "write-turn2",
                "name": "write_file",
                "content": json.dumps({"bytes_written": 10, "resolved_path": str(artifact)}),
            },
            {"role": "assistant", "content": f"已生成 `{artifact}`"},
        ],
        tool_calls=[{
            "name": "write_file",
            "tid": "write-turn2",
            "assistant_msg_idx": 9,
            "args": {"path": str(artifact)},
            "snippet": '{"bytes_written": 10}',
        }],
    )


def test_manual_cron_turn_persists_write_file_artifact(tmp_path, monkeypatch):
    from integration.session_manifest.store import load_manifest_records
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _manual_cron_session(workspace)
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")

    result = _persist_turn_artifact_paths(session, "turn:2")

    assert result == {
        "status": "persisted",
        "decision": "artifacts",
        "turn_key": "turn:2",
        "artifact_count": 1,
    }
    assert [(row["turn_key"], row["path"], row["source_tool"], row["preview"])
            for row in load_manifest_records(session)] == [
        ("turn:2", "gpt-5.6-latest-summary.md", "write_file", "file"),
    ]


def test_artifact_extraction_failure_is_observable(tmp_path, monkeypatch, caplog):
    from integration.session_manifest import manifest as session_manifest
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _manual_cron_session(workspace)

    def fail_extract(*_args, **_kwargs):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(session_manifest, "extract_turn_artifact_entries_for_manifest", fail_extract)
    with caplog.at_level(logging.WARNING):
        result = _persist_turn_artifact_paths(session, "turn:2")

    assert result == {"status": "failed", "stage": "extract", "turn_key": "turn:2", "artifact_count": 0}
    assert "Manifest artifact extraction failed" in caplog.text
    assert "session=cron_manual_artifact01 turn=turn:2" in caplog.text


def test_artifact_store_failure_is_observable_not_empty(tmp_path, monkeypatch, caplog):
    from integration.session_manifest import store as session_manifest_store
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _manual_cron_session(workspace)

    def fail_store(*_args, **_kwargs):
        raise OSError("store unavailable")

    monkeypatch.setattr(session_manifest_store, "upsert_manifest_records", fail_store)
    with caplog.at_level(logging.WARNING):
        result = _persist_turn_artifact_paths(session, "turn:2")

    assert result == {"status": "failed", "stage": "store", "turn_key": "turn:2", "artifact_count": 1}
    assert "Manifest artifact store write failed" in caplog.text
    assert "session=cron_manual_artifact01 turn=turn:2 entries=1" in caplog.text
