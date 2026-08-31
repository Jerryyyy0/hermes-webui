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
    from integration.session_manifest.store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "yijing_pro.html"
    artifact.write_text("<html></html>", encoding="utf-8")
    session = _session(workspace)
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
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


def test_settlement_dedupes_stream_and_transcript_evidence_by_stronger_source(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.html"
    artifact.write_text("<html></html>", encoding="utf-8")
    session = Session(
        session_id="artifact-source-priority",
        workspace=str(workspace),
        active_stream_id="stream-priority",
        messages=[
            {"role": "user", "content": "生成报告", "_turn_key": "turn:1"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "write-report",
                    "function": {"name": "write_file", "arguments": '{"path":"report.html"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "write-report", "content": "saved"},
            {"role": "assistant", "content": f"MEDIA:{artifact}"},
        ],
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-priority"] = {
        "artifacts": [{
            "turn_key": "turn:1",
            "path": "report.html",
            "source_tool": "media",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:1",
        stream_id="stream-priority",
    )

    assert result["status"] == "persisted"
    assert [(row["path"], row["source_tool"]) for row in load_manifest_records(session)] == [
        ("report.html", "write_file"),
    ]


def test_completed_settlement_drops_stream_artifact_removed_before_turn_end(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.manifest import build_session_manifest
    from integration.session_manifest.store import (
        load_manifest_decided_turn_keys,
        load_manifest_records,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_preview = workspace / "preview-cover.jpg"
    old_preview.write_bytes(b"jpg")
    old_preview.rename(workspace / "preview-cover.png")
    session = Session(
        session_id="artifact-removed-before-complete",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-complete",
        messages=[
            {"role": "user", "content": "生成预览", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "完成"},
        ],
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-complete"] = {
        "artifacts": [{
            "turn_key": "turn:1",
            "path": "preview-cover.jpg",
            "source_tool": "terminal",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:1",
        stream_id="stream-complete",
        terminal_reason="completed",
    )

    assert result == {
        "status": "persisted",
        "decision": "empty",
        "turn_key": "turn:1",
        "artifact_count": 0,
    }
    assert load_manifest_records(session) == []
    assert load_manifest_decided_turn_keys(session) == {"turn:1"}
    manifest = build_session_manifest(session)
    assert manifest["artifacts"] == []
    assert manifest["turns"][0]["artifacts"] == []


def test_cancelled_settlement_does_not_persist_removed_stream_artifact(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import (
        load_manifest_decided_turn_keys,
        load_manifest_records,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace)
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-old"] = {
        "artifacts": [{
            "turn_key": "turn:7",
            "path": "preview-cover.jpg",
            "source_tool": "terminal",
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
        "status": "pending",
        "stage": "evidence_unsettled",
        "turn_key": "turn:7",
        "artifact_count": 0,
    }
    assert load_manifest_records(session) == []
    assert load_manifest_decided_turn_keys(session) == set()


def test_synthetic_verification_nudge_does_not_block_bound_artifact_settlement(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.png"
    artifact.write_bytes(b"png")
    session = Session(
        session_id="artifact-synthetic-nudge",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-synthetic",
        messages=[
            {"role": "user", "content": "生成图片", "_turn_key": "turn:8"},
            {"role": "assistant", "content": "premature done"},
            {
                "role": "user",
                "content": "[System: run verification]",
                "_verification_stop_synthetic": True,
            },
            {"role": "assistant", "content": "MEDIA: result.png"},
        ],
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-synthetic"] = {
        "artifacts": [{
            "turn_key": "turn:8",
            "path": "result.png",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:8",
        stream_id="stream-synthetic",
        terminal_reason="completed",
        expected_user_text="生成图片",
    )

    assert result == {
        "status": "persisted",
        "decision": "artifacts",
        "turn_key": "turn:8",
        "artifact_count": 1,
    }
    assert [(row["turn_key"], row["path"]) for row in load_manifest_records(session)] == [
        ("turn:8", "result.png"),
    ]


def test_new_semantic_scaffold_does_not_block_bound_artifact_settlement(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_records

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.png"
    artifact.write_bytes(b"png")
    session = Session(
        session_id="artifact-semantic-scaffold",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-semantic",
        messages=[
            {"role": "user", "content": "生成图片", "_turn_key": "turn:8"},
            {"role": "assistant", "content": "I will verify this."},
            {
                "role": "user",
                "content": "[System: continue]",
                "_hermes_message_class": "internal_scaffold",
                "_hermes_scaffold_kind": "length_continuation",
            },
            {"role": "assistant", "content": "MEDIA: result.png"},
        ],
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    streaming.STREAM_LIVE_MANIFEST["stream-semantic"] = {
        "artifacts": [{
            "turn_key": "turn:8",
            "path": "result.png",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:8",
        stream_id="stream-semantic",
        terminal_reason="completed",
        expected_user_text="生成图片",
    )

    assert result["status"] == "persisted"
    assert [(row["turn_key"], row["path"]) for row in load_manifest_records(session)] == [
        ("turn:8", "result.png"),
    ]


def test_final_assistant_existing_file_augments_live_stream_evidence(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_records

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
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
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
    from integration.session_manifest.store import load_manifest_decided_turn_keys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(workspace, active_stream_id="stream-new")
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")

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


def test_current_turn_key_conflict_cannot_settle_artifacts(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_decided_turn_keys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "recolor.py"
    artifact.write_text("print('ok')", encoding="utf-8")
    session = Session(
        session_id="artifact-turn-conflict",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-current",
        messages=[
            {"role": "user", "content": "配色淡一点", "_turn_key": "turn:7"},
            {"role": "assistant", "content": "完成"},
        ],
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    journal_events = []
    monkeypatch.setattr(
        streaming,
        "append_turn_journal_event_for_stream",
        lambda session_id, stream_id, event: journal_events.append((session_id, stream_id, event)),
    )
    streaming.STREAM_LIVE_MANIFEST["stream-current"] = {
        "artifacts": [{
            "turn_key": "turn:6",
            "path": "recolor.py",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    }

    result = streaming._persist_turn_artifact_paths(
        session,
        "turn:6",
        stream_id="stream-current",
        terminal_reason="completed",
        expected_user_text="配色淡一点",
    )

    assert result == {
        "status": "failed",
        "stage": "turn_key_conflict",
        "turn_key": "turn:6",
        "actual_turn_key": "turn:7",
        "artifact_count": 0,
    }
    assert load_manifest_decided_turn_keys(session) == set()
    assert len(journal_events) == 1
    journal_session_id, journal_stream_id, journal_event = journal_events[0]
    assert journal_session_id == session.session_id
    assert journal_stream_id == "stream-current"
    assert journal_event["event"] == "artifact_persistence_failed"
    assert journal_event["expected_turn_key"] == "turn:6"
    assert journal_event["actual_turn_key"] == "turn:7"
    assert journal_event["stage"] == "turn_key_conflict"
    assert journal_event["terminal_reason"] == "completed"


def test_passive_compression_rotation_keeps_canonical_turn_artifact_alignment(tmp_path, monkeypatch):
    from integration.session_manifest import store
    from api import streaming
    from integration.session_manifest.manifest import build_session_manifest

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before", encoding="utf-8")
    (workspace / "recolor.py").write_text("print('ok')", encoding="utf-8")

    previous_display = [
        {"role": "user", "content": "准备页面", "_turn_key": "turn:1"},
        {"role": "assistant", "content": "已准备"},
        {"role": "user", "content": "配色淡一点", "_turn_key": "turn:6"},
        {"role": "assistant", "content": "处理中", "_partial": True},
    ]
    result_messages = [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION - REFERENCE ONLY] Earlier turns were compacted.",
        },
        {"role": "user", "content": "配色淡一点", "id": 103},
        {"role": "assistant", "content": "已生成 recolor.py"},
    ]
    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_display[:2],
        result_messages,
        "配色淡一点",
        canonical_turn_key="turn:6",
    )

    current_users = [
        row for row in merged
        if row.get("role") == "user" and row.get("content") == "配色淡一点"
    ]
    assert current_users == [{
        "role": "user",
        "content": "配色淡一点",
        "_turn_key": "turn:6",
        "id": 103,
    }]

    state_dir = tmp_path / "state"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "compression-parent.json").write_text(
        '{"session_id":"compression-parent","pre_compression_snapshot":true,"messages":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "STATE_DIR", state_dir)
    monkeypatch.setattr(store, "SESSION_DIR", session_dir)
    monkeypatch.setattr("api.workspace._BOOT_DEFAULT_WORKSPACE", workspace)
    monkeypatch.setattr(
        "integration.session_manifest.manifest._load_display_messages",
        lambda session: list(session.messages),
    )

    parent = Session(
        session_id="compression-parent",
        workspace=str(workspace),
        profile="ops",
        pre_compression_snapshot=True,
    )
    store.upsert_manifest_records(
        parent,
        "turn:1",
        [{"path": "before.txt", "source_tool": "write_file"}],
    )

    continuation = Session(
        session_id="compression-child",
        parent_session_id="compression-parent",
        workspace=str(workspace),
        profile="ops",
        active_stream_id="stream-compression",
        messages=merged,
    )
    monkeypatch.setitem(streaming.STREAM_LIVE_MANIFEST, "stream-compression", {
        "artifacts": [{
            "turn_key": "turn:6",
            "path": "recolor.py",
            "source_tool": "write_file",
            "preview": "file",
        }],
        "turns": [],
    })

    settlement = streaming._persist_turn_artifact_paths(
        continuation,
        "turn:6",
        stream_id="stream-compression",
        terminal_reason="completed",
        expected_user_text="配色淡一点",
    )
    manifest = build_session_manifest(continuation)
    turns = {row["turn_key"]: row for row in manifest["turns"]}

    assert settlement == {
        "status": "persisted",
        "decision": "artifacts",
        "turn_key": "turn:6",
        "artifact_count": 1,
    }
    assert [row["path"] for row in turns["turn:1"]["artifacts"]] == ["before.txt"]
    assert [row["path"] for row in turns["turn:6"]["artifacts"]] == ["recolor.py"]
    assert manifest["diagnostics"]["orphan_turn_keys"] == []


def test_legacy_empty_turn_nine_repairs_from_same_turn_write_file(tmp_path):
    from integration.session_manifest import store

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
