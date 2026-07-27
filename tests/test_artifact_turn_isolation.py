"""Regression coverage for run-owned artifact settlement."""

from __future__ import annotations

from types import SimpleNamespace

from api.models import Session


def _session(workspace, *, active_stream_id="stream-old"):
    return Session(
        session_id="artifact-turn-isolation",
        workspace=str(workspace),
        profile="ops",
        active_stream_id=active_stream_id,
        messages=[
            {"role": "user", "content": "cancelled turn", "_turn_key": "turn:7"},
            {"role": "assistant", "content": "cancelled"},
            {"role": "user", "content": "current turn", "_turn_key": "turn:9"},
        ],
    )


def test_stream_owned_tool_evidence_settles_bound_turn_before_transcript_merge(tmp_path, monkeypatch):
    from api import streaming
    from api.session_manifest_store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "yijing_pro.html"
    artifact.write_text("<html></html>", encoding="utf-8")
    session = _session(workspace)
    monkeypatch.setattr("api.session_manifest_store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-old"] = {
        "artifacts": [{
            "turn_key": "turn:7",
            "path": "yijing_pro.html",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:7",
        stream_id="stream-old",
        terminal_reason="cancelled",
    )

    assert result == {
        "status": "persisted",
        "decision": "artifacts",
        "turn_key": "turn:7",
        "artifact_count": 1,
    }
    assert [(row["turn_key"], row["path"]) for row in load_manifest_records(session)] == [
        ("turn:7", "yijing_pro.html"),
    ]


def test_final_assistant_existing_file_augments_live_stream_evidence(tmp_path, monkeypatch):
    from api import streaming
    from api.session_manifest_store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    html = workspace / "summary.html"
    pdf = workspace / "summary.pdf"
    html.write_text("<html></html>", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.7")
    session = Session(
        session_id="final-prose-artifact",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-final-prose",
        messages=[
            {"role": "user", "content": "make PDF", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "working"},
            {"role": "assistant", "content": "Created `summary.pdf`"},
        ],
    )
    monkeypatch.setattr("api.session_manifest_store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-final-prose"] = {
        "artifacts": [{
            "turn_key": "turn:1",
            "path": "summary.html",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:1",
        stream_id="stream-final-prose",
        terminal_reason="completed",
    )

    assert result["artifact_count"] == 2
    assert [(row["turn_key"], row["path"]) for row in load_manifest_records(session)] == [
        ("turn:1", "summary.html"),
        ("turn:1", "summary.pdf"),
    ]


def test_stale_worker_cannot_settle_or_create_empty_decision(tmp_path, monkeypatch):
    from api import streaming
    from api.session_manifest_store import load_manifest_decided_turn_keys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace, active_stream_id="stream-new")
    monkeypatch.setattr("api.session_manifest_store.STATE_DIR", tmp_path / "state")

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:7",
        stream_id="stream-old",
        terminal_reason="completed",
    )

    assert result == {
        "status": "pending",
        "stage": "stale_worker",
        "turn_key": "turn:7",
        "artifact_count": 0,
    }
    assert load_manifest_decided_turn_keys(session) == set()


def test_legacy_empty_turn_nine_repairs_from_same_turn_write_file(tmp_path):
    from api import session_manifest_store as store

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "yijing_pro.html"
    artifact.write_text("<html></html>", encoding="utf-8")
    session = SimpleNamespace(
        session_id="legacy-turn-nine",
        profile="ops",
        workspace=str(workspace),
        parent_session_id=None,
        pre_compression_snapshot=False,
        tool_calls=[{
            "name": "write_file",
            "tid": "write-turn-nine",
            "assistant_msg_idx": 4,
            "args": {"path": str(artifact)},
            "done": True,
        }],
        messages=[
            {"role": "user", "content": "cancelled", "_turn_key": "turn:7"},
            {"role": "assistant", "content": "cancelled"},
            {"role": "user", "content": "write page", "_turn_key": "turn:9"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "write-turn-nine",
                "function": {"name": "write_file", "arguments": '{"path": "yijing_pro.html"}'},
            }]},
            {"role": "tool", "tool_call_id": "write-turn-nine", "name": "write_file", "content": "ok"},
            {"role": "assistant", "content": "created yijing_pro.html"},
        ],
    )
    db_path = tmp_path / "manifest.db"
    store.upsert_manifest_records(session, "turn:7", [{"path": "", "source_tool": "assistant_prose"}], db_path=db_path)
    store.upsert_manifest_records(session, "turn:9", [{"path": "", "source_tool": "assistant_prose"}], db_path=db_path)

    assert store.repair_empty_manifest_turns(session, db_path=db_path) == 1
    assert store.load_manifest_empty_turn_keys(session, db_path=db_path) == {"turn:7"}
    assert [(row["turn_key"], row["path"]) for row in store.load_manifest_records(session, db_path=db_path)] == [
        ("turn:9", "yijing_pro.html"),
    ]
