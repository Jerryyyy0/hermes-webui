"""Regression coverage for workspace defaults and global-root artifacts."""

from pathlib import Path

from api.models import Session
from integration.session_manifest.manifest import ToolEvent, build_session_manifest, extract_manifest_delta_from_tool_event
from api.streaming import _webui_ephemeral_system_prompt


def test_workspace_policy_is_the_final_ephemeral_instruction():
    workspace = "/tmp/hermes-workspace/sessions/turn-1"

    prompt = _webui_ephemeral_system_prompt(
        None,
        surface_context={"source": "webui", "workspace": workspace},
        config_data={},
    )

    assert "Workspace resolution policy — applies to this request:" in prompt
    assert f"The current session workspace is: {workspace}" in prompt
    assert "It may be outside\n   the session workspace; honor it" in prompt
    assert prompt.rstrip().endswith("unless the user explicitly requests that path.")


def test_global_workspace_write_is_a_turn_artifact_for_managed_session(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    session_workspace = workspace_root / "sessions" / "managed-artifact"
    session_workspace.mkdir(parents=True)
    report = workspace_root / "brief.md"
    report.write_text("# briefing", encoding="utf-8")
    monkeypatch.setattr(
        "api.workspace.resolve_trusted_workspace",
        lambda path=None: workspace_root.resolve()
        if path in (None, "")
        else Path(path).expanduser().resolve(),
    )
    session = Session(
        session_id="managed-artifact",
        workspace=str(session_workspace),
        messages=[
            {"role": "user", "content": "生成简报", "_turn_key": "turn:0"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "write-brief",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "' + str(report) + '"}',
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "write-brief", "content": "ok"},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr("integration.session_manifest.manifest._load_display_messages", lambda s: list(s.messages))

    manifest = build_session_manifest(session)

    expected = {"path": "brief.md", "preview": "file", "source_tool": "write_file"}
    assert manifest["artifacts"] == [expected]
    assert manifest["turns"][0]["artifacts"] == [expected]


def test_global_workspace_write_uses_same_root_for_sse_and_store(tmp_path, monkeypatch):
    from api import streaming
    from integration.session_manifest.store import load_manifest_records

    workspace_root = tmp_path / "workspace"
    session_workspace = workspace_root / "sessions" / "managed-persist"
    session_workspace.mkdir(parents=True)
    report = workspace_root / "brief.md"
    report.write_text("# briefing", encoding="utf-8")
    monkeypatch.setattr(
        "api.workspace.resolve_trusted_workspace",
        lambda path=None: workspace_root.resolve()
        if path in (None, "")
        else Path(path).expanduser().resolve(),
    )
    monkeypatch.setattr("integration.session_manifest.store.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr("integration.session_manifest.manifest._load_display_messages", lambda s: list(s.messages))
    session = Session(
        session_id="managed-persist",
        workspace=str(session_workspace),
        messages=[
            {"role": "user", "content": "生成简报", "_turn_key": "turn:0"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "write-brief",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path": "' + str(report) + '"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "write-brief",
                "name": "write_file",
                "content": '{"bytes_written": 10, "resolved_path": "' + str(report) + '"}',
            },
        ],
        tool_calls=[{
            "name": "write_file",
            "tid": "write-brief",
            "assistant_msg_idx": 1,
            "args": {"path": str(report)},
            "done": True,
        }],
    )

    delta = extract_manifest_delta_from_tool_event(
        ToolEvent(
            name="write_file",
            args={"path": str(report)},
            result="ok",
            tid="write-brief",
            status="completed",
        ),
        session_workspace,
        session_id=session.session_id,
        turn_key="turn:0",
        artifact_workspace=workspace_root,
    )
    settlement = streaming._persist_turn_artifact_paths(session, "turn:0")

    expected = {"path": "brief.md", "preview": "file", "source_tool": "write_file"}
    assert delta["artifacts"] == [expected]
    assert settlement["status"] == "persisted"
    assert [(row["workspace_root"], row["path"]) for row in load_manifest_records(session)] == [
        (str(workspace_root), "brief.md"),
    ]
    assert build_session_manifest(session)["artifacts"] == [expected]
