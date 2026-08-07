import argparse
import json
import random
import sqlite3

import pytest

from scripts.real_model_campaign import (
    HistoryPrompt,
    _run_round,
    _wait_for_chat_ready,
    build_history_prompt,
    cancellation_plan,
    cleanup_campaign_test_data,
    drain_blocking_prompts,
    enable_auto_approve,
    evaluate_alignment,
    immediate_cancel_plan,
    list_campaign_test_sessions,
    load_history_prompt_pool,
    load_prefix_messages,
    parse_bool_arg,
    pick_history_prompt,
    pool_for_context_mode,
    resolve_batch_specs,
    run_campaign,
    sanitize_prefix_messages,
    seed_workspace,
)


class _FakeApi:
    def __init__(self):
        self.calls = []
        self.approval_queue = [{"approval_id": "a1"}]
        self.clarify_queue = [{"clarify_id": "c1"}]

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if path.startswith("/api/approval/pending"):
            item = self.approval_queue.pop(0) if self.approval_queue else None
            return {"pending": item, "pending_count": 1 if item else 0}
        if path.startswith("/api/clarify/pending"):
            item = self.clarify_queue.pop(0) if self.clarify_queue else None
            return {"pending": item}
        return {"ok": True}


class _ReadinessApi:
    def __init__(self, readiness):
        self.calls = []
        self.readiness = iter(readiness)

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if path.startswith("/api/session/status"):
            return {"can_start_chat": next(self.readiness)}
        raise AssertionError(f"unexpected request: {method} {path}")


def test_default_cancel_plan_covers_all_paths():
    assert cancellation_plan(15) == {
        3: "immediate_after_start",
        6: "after_first_tool",
        8: "after_manifest_delta",
        12: "after_first_artifact",
    }


def test_immediate_cancel_plan_covers_every_turn():
    assert immediate_cancel_plan(5) == {
        1: "immediate_after_start",
        2: "immediate_after_start",
        3: "immediate_after_start",
        4: "immediate_after_start",
        5: "immediate_after_start",
    }


def test_parse_bool_arg_accepts_common_truthy_falsy_values():
    assert parse_bool_arg("true") is True
    assert parse_bool_arg("FALSE") is False
    assert parse_bool_arg("1") is True
    assert parse_bool_arg("0") is False
    with pytest.raises(argparse.ArgumentTypeError):
        parse_bool_arg("maybe")


def test_wait_for_chat_ready_waits_for_authoritative_status(monkeypatch):
    api = _ReadinessApi([False, True])
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)

    status = _wait_for_chat_ready(api, "sid-1")

    assert status == {"can_start_chat": True}
    assert [path for _method, path, _body in api.calls] == [
        "/api/session/status?session_id=sid-1",
        "/api/session/status?session_id=sid-1",
    ]


def test_unready_session_fails_campaign_round_without_starting_chat(monkeypatch, tmp_path):
    question = HistoryPrompt("source", "title", "请创建交付文件", 1, "turn:0", 5, 1, ())
    api = _FakeApi()
    monkeypatch.setattr(
        "scripts.real_model_campaign._wait_for_chat_ready",
        lambda *_args: {"can_start_chat": False},
    )

    result = _run_round(
        api,
        "sid-1",
        tmp_path,
        question,
        "campaign-1",
        1,
        None,
    )

    assert result["observations"] == [{"code": "SESSION_NOT_READY"}]
    assert result["alignment_failures"] == [{"code": "SESSION_NOT_READY"}]
    assert not api.calls


class _CancelFlowApi:
    """Minimal API stub for stream-open/cancel/status behavior."""

    def __init__(self, stream_events):
        self.calls = []
        self.stream_events = list(stream_events)
        self.events_opened = 0
        self.stream_connections = []

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if path == "/health":
            return {"status": "ok", "active_streams": [], "active_runs": []}
        if path == "/api/session/new":
            return {"session": {"session_id": "sid-1"}}
        if path.startswith("/api/session/status"):
            return {"can_start_chat": True}
        if path.startswith("/api/session/yolo"):
            return {"ok": True}
        if path == "/api/chat/start":
            return {"stream_id": "stream-1", "session_id": "sid-1"}
        if path.startswith("/api/chat/cancel"):
            return {"ok": True, "cancelled": True, "settled": False, "stream_id": "stream-1"}
        if path.startswith("/api/approval/pending") or path.startswith("/api/clarify/pending"):
            return {"pending": None}
        if path.startswith("/api/session?session_id="):
            return {"session": {"messages": [{"role": "user", "content": "nonce-marker", "_turn_key": "turn:1"}]}}
        if path.startswith("/api/session/manifest"):
            return {"manifest": {"turns": [], "diagnostics": {"orphan_turn_keys": []}}}
        return {"ok": True}

    def events(self, stream_id):
        self.events_opened += 1
        yield from self.stream_events

    def open_event_stream(self, stream_id):
        connection = _FakeStreamConnection()
        self.calls.append(("GET", f"/api/chat/stream?stream_id={stream_id}", None))
        self.stream_connections.append(connection)
        return connection


class _FakeStreamConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_immediate_cancel_uses_known_ready_state_then_polls_next_round_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    api = _CancelFlowApi(stream_events=[("token", {"text": "should-not-see"}), ("done", {})])
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))
    # Force a stable nonce so session lookup in evaluate_alignment can be skipped via empty turn_key path
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())

    row = _run_round(
        api,
        "sid-1",
        tmp_path,
        question,
        "camp-1",
        1,
        "immediate_after_start",
        start_ready=True,
    )

    assert api.events_opened == 0
    assert len(api.stream_connections) == 1
    assert api.stream_connections[0].closed is True
    assert row["cancel"]["cancelled"] is True
    paths = [path for _method, path, _body in api.calls]
    start_idx = next(i for i, path in enumerate(paths) if path == "/api/chat/start")
    stream_idx = next(i for i, path in enumerate(paths) if path.startswith("/api/chat/stream"))
    cancel_idx = next(i for i, path in enumerate(paths) if path.startswith("/api/chat/cancel"))
    assert not any(path.startswith("/api/session/status") for path in paths[:start_idx])
    assert stream_idx < cancel_idx
    status_after = [path for path in paths[cancel_idx + 1 :] if path.startswith("/api/session/status")]
    assert status_after, "status poll must follow successful cancel"
    assert row["ready_for_next_start"] is True
    assert "token" not in {event["event"] for event in row["events"]}


def test_cancel_verify_reuses_post_cancel_readiness_for_next_start(tmp_path, monkeypatch):
    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: api)
    monkeypatch.setattr("scripts.real_model_campaign._state_dir", lambda: tmp_path)
    monkeypatch.setattr("scripts.real_model_campaign.load_history_prompt_pool", lambda _state_dir: [question])
    monkeypatch.setattr("scripts.real_model_campaign.pool_for_context_mode", lambda pool, _mode: pool)
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())

    api = _CancelFlowApi(stream_events=[])
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))

    assert run_campaign(1, 2, "http://test", context_mode="first", cancel_verify_session=True) == 0

    paths = [path for _method, path, _body in api.calls]
    new_idx = paths.index("/api/session/new")
    start_indices = [index for index, path in enumerate(paths) if path == "/api/chat/start"]
    cancel_indices = [index for index, path in enumerate(paths) if path.startswith("/api/chat/cancel")]
    status_indices = [index for index, path in enumerate(paths) if path.startswith("/api/session/status")]
    assert len(start_indices) == len(cancel_indices) == 2
    assert new_idx < start_indices[0]
    assert not any(new_idx < index < start_indices[0] for index in status_indices)
    assert cancel_indices[0] < status_indices[0] < start_indices[1]


def test_midstream_cancel_stops_reading_sse(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    api = _CancelFlowApi(stream_events=[
        ("tool", {"name": "read_file"}),
        ("token", {"text": "after-cancel-should-not-be-consumed"}),
        ("done", {}),
    ])
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())

    row = _run_round(api, "sid-1", tmp_path, question, "camp-1", 1, "after_first_tool")

    assert api.events_opened == 1
    assert [event["event"] for event in row["events"]] == ["tool"]
    assert row["cancel"]["cancelled"] is True
    paths = [path for _method, path, _body in api.calls]
    cancel_idx = next(i for i, path in enumerate(paths) if path.startswith("/api/chat/cancel"))
    assert any(path.startswith("/api/session/status") for path in paths[cancel_idx + 1 :])


def test_resolve_batch_specs_cancel_verify_is_first_session_when_enabled():
    assert [spec["kind"] for spec in resolve_batch_specs(2, 5)] == ["normal", "normal"]
    specs = resolve_batch_specs(2, 5, cancel_verify_session=True)
    assert [spec["kind"] for spec in specs] == ["cancel_verify", "normal"]
    assert specs[0]["batch_index"] == 1
    assert specs[0]["cancel_plan"] == immediate_cancel_plan(5)
    assert resolve_batch_specs(1, 5, cancel_verify_session=True)[0]["kind"] == "cancel_verify"


def test_history_prompt_wraps_nonce_and_turn_path():
    question = HistoryPrompt(
        source_session_id="abc123",
        title="demo",
        prompt="给我一个可视化分析报告html",
        turn=1,
        turn_key="turn:0",
        n_tools=8,
        n_write_tools=2,
        artifact_paths=("report.html",),
    )
    prompt, path = build_history_prompt(question, 3, "nonce-3")
    assert "给我一个可视化分析报告html" in prompt
    assert "NONCE=nonce-3" in prompt
    assert path.endswith("turn-03/delivery.md")


def test_seed_workspace_creates_constraints(tmp_path):
    seed_workspace(tmp_path)
    assert (tmp_path / "constraints.md").exists()
    assert (tmp_path / "deliverables").is_dir()


def test_pick_history_prompt_is_deterministic_with_seed():
    pool = [
        HistoryPrompt("s1", "t1", "问题一请生成 md", 1, "turn:0", 6, 1, ("a.md",)),
        HistoryPrompt("s2", "t2", "问题二请生成 html", 1, "turn:0", 7, 2, ("b.html",)),
        HistoryPrompt("s3", "t3", "问题三请生成报告", 2, "turn:1", 9, 1, ("c.md",), user_msg_index=4),
    ]
    a = pick_history_prompt(pool, random.Random(7))
    b = pick_history_prompt(pool, random.Random(7))
    assert a == b


def test_pool_for_context_mode_splits_first_and_replay():
    pool = [
        HistoryPrompt("s1", "t1", "首轮问题生成 md", 1, "turn:0", 6, 1, ("a.md",)),
        HistoryPrompt("s2", "t2", "中间轮再生成 html", 3, "turn:2", 8, 2, ("b.html",), user_msg_index=5),
    ]
    assert [p.source_session_id for p in pool_for_context_mode(pool, "first")] == ["s1"]
    assert [p.source_session_id for p in pool_for_context_mode(pool, "replay")] == ["s2"]
    assert len(pool_for_context_mode(pool, "mixed")) == 2


def test_sanitize_prefix_messages_keeps_or_assigns_turn_keys_and_truncates_tools():
    rows = sanitize_prefix_messages([
        {"role": "user", "content": "hi", "_turn_key": "turn:0"},
        {"role": "user", "content": "没有 key 的历史问题"},
        {"role": "user", "content": "[Your active task list was preserved across context compression]\n- [ ] x"},
        {"role": "tool", "content": "x" * 20000, "tool_call_id": "c1"},
    ])
    assert rows[0]["_turn_key"] == "turn:0"
    assert rows[1]["_turn_key"] == "import:0"
    assert "_turn_key" not in rows[2]
    assert rows[3]["content"].endswith("…[truncated for campaign replay]…")
    assert len(rows[3]["content"]) < 13000


def _write_tools(ids_prefix: str):
    return [
        {"id": f"{ids_prefix}{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for i, name in enumerate(("terminal", "read_file", "search_files", "todo", "write_file"), start=1)
    ]


def test_load_history_prompt_pool_keeps_first_and_mid_turns(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    sid = "hist001abc"
    messages = [
        {"role": "user", "content": "分析日志并生成可视化 html 报告", "_turn_key": "turn:0"},
        {"role": "assistant", "content": "", "tool_calls": _write_tools("c")},
        {"role": "tool", "tool_call_id": "c5", "name": "write_file", "content": "ok"},
        {"role": "user", "content": "再给我一个 word 版本", "_turn_key": "turn:1"},
        {"role": "assistant", "content": "", "tool_calls": _write_tools("d")},
        {"role": "tool", "tool_call_id": "d5", "name": "write_file", "content": "ok"},
    ]
    (sessions / f"{sid}.json").write_text(
        json.dumps({
            "session_id": sid,
            "title": "日志分析",
            "workspace": str(tmp_path / "ws"),
            "messages": messages,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    db = tmp_path / "session_manifest.db"
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE session_manifest_records (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            lineage_key TEXT,
            profile TEXT,
            turn_key TEXT,
            record_kind TEXT,
            path TEXT,
            preview TEXT,
            source_tool TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    con.executemany(
        """
        INSERT INTO session_manifest_records
        (session_id, lineage_key, profile, turn_key, record_kind, path, preview, source_tool, created_at, updated_at)
        VALUES (?, '', '', ?, 'artifact', ?, '', 'write_file', '', '')
        """,
        [(sid, "turn:0", "report.html"), (sid, "turn:1", "report.docx")],
    )
    con.commit()
    con.close()

    pool = load_history_prompt_pool(tmp_path)
    assert {p.turn for p in pool} == {1, 2}
    mid = next(p for p in pool if p.turn == 2)
    assert mid.user_msg_index == 3
    assert mid.needs_prefix_replay
    prefix = load_prefix_messages(tmp_path, mid)
    assert len(prefix) == 3
    assert prefix[0]["role"] == "user"
    assert prefix[0].get("_turn_key") == "turn:0"


def test_enable_auto_approve_posts_session_yolo():
    api = _FakeApi()
    assert enable_auto_approve(api, "sid-1")["ok"] is True
    assert api.calls[0][:2] == ("POST", "/api/session/yolo")
    assert api.calls[0][2] == {"session_id": "sid-1", "enabled": True}


def test_cleanup_campaign_test_data_deletes_sessions_and_artifact_dirs(tmp_path):
    campaigns = tmp_path / "e2e_campaigns"
    ws = campaigns / "20260806-demo" / "artifacts" / "batch-01" / "plain"
    ws.mkdir(parents=True)
    (ws / "constraints.md").write_text("x", encoding="utf-8")
    (ws / "deliverables").mkdir()
    (ws / "deliverables" / "delivery.md").write_text("leak", encoding="utf-8")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "camp01.json").write_text(
        json.dumps({
            "session_id": "camp01",
            "title": "历史首轮问题包装后的标题",
            "workspace": str(ws),
            "messages": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (sessions / "keepme.json").write_text(
        json.dumps({
            "session_id": "keepme",
            "title": "正常会话",
            "workspace": str(tmp_path / "other-ws"),
            "messages": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert [row["session_id"] for row in list_campaign_test_sessions(tmp_path)] == ["camp01"]
    api = _FakeApi()
    result = cleanup_campaign_test_data(api, tmp_path)
    assert result["ok"] is True
    assert result["deleted_sessions"] == ["camp01"]
    assert "20260806-demo" in result["removed_campaign_dirs"]
    assert api.calls[0][:2] == ("POST", "/api/session/delete")
    assert not (campaigns / "20260806-demo").exists()
    assert (sessions / "keepme.json").is_file()


def test_drain_blocking_prompts_resolves_approval_and_clarify():
    api = _FakeApi()
    counts = drain_blocking_prompts(api, "sid-1")
    assert counts == {"approvals": 1, "clarifies": 1}
    paths = [path for _method, path, _body in api.calls]
    assert "/api/approval/respond" in paths
    assert "/api/clarify/respond" in paths


def test_missing_artifact_is_not_an_alignment_failure(tmp_path):
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {"turns": [{"turn_key": "turn:1", "artifacts": []}], "diagnostics": {"orphan_turn_keys": []}}
    failures, observations, _ = evaluate_alignment(session, manifest, {"nonce": "nonce", "turn_key": "turn:1"}, tmp_path)
    assert not failures and observations == [{"code": "MODEL_NO_ARTIFACT"}]


def test_turn_key_ignores_nonce_echo_in_tool_messages(tmp_path):
    delivery = tmp_path / "deliverables/turn-01/delivery.md"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("NONCE=nonce\n", encoding="utf-8")
    session = {
        "messages": [
            {"role": "user", "content": "请交付 NONCE=nonce", "_turn_key": "turn:1"},
            {"role": "assistant", "content": "将写入 NONCE=nonce"},
            {"role": "tool", "content": "wrote deliverables/turn-01/delivery.md NONCE=nonce"},
        ]
    }
    manifest = {
        "turns": [{"turn_key": "turn:1", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}]}],
        "diagnostics": {"orphan_turn_keys": []},
    }
    failures, observations, hashes = evaluate_alignment(
        session, manifest, {"nonce": "nonce", "expected_artifact_path": "deliverables/turn-01/delivery.md"}, tmp_path,
    )
    assert not failures and not observations
    assert "deliverables/turn-01/delivery.md" in hashes


def test_companion_html_without_nonce_is_not_failure(tmp_path):
    delivery = tmp_path / "deliverables/turn-01/delivery.md"
    html = tmp_path / "deliverables/turn-01/report.html"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("NONCE=nonce\n", encoding="utf-8")
    html.write_text("<html></html>", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "NONCE=nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [{
            "turn_key": "turn:1",
            "artifacts": [
                {"path": "deliverables/turn-01/delivery.md"},
                {"path": "deliverables/turn-01/report.html"},
            ],
        }],
        "diagnostics": {"orphan_turn_keys": []},
    }
    failures, observations, _ = evaluate_alignment(session, manifest, {"nonce": "nonce"}, tmp_path)
    assert not failures and not observations


def test_seed_constraints_multi_owner_is_ignored(tmp_path):
    delivery = tmp_path / "deliverables/turn-01/delivery.md"
    constraints = tmp_path / "constraints.md"
    delivery.parent.mkdir(parents=True)
    delivery.write_text("NONCE=nonce\n", encoding="utf-8")
    constraints.write_text("seed\n", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "NONCE=nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [
            {"turn_key": "turn:1", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}, {"path": "constraints.md"}]},
            {"turn_key": "turn:2", "artifacts": [{"path": "constraints.md"}]},
        ],
        "diagnostics": {"orphan_turn_keys": []},
    }
    failures, _, _ = evaluate_alignment(session, manifest, {"nonce": "nonce"}, tmp_path)
    assert not any(issue["code"] == "ARTIFACT_MULTI_TURN_OWNER" for issue in failures)


def test_artifact_with_two_turn_owners_fails_alignment(tmp_path):
    file = tmp_path / "deliverables/turn-01/delivery.md"
    file.parent.mkdir(parents=True)
    file.write_text("nonce", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [
            {"turn_key": "turn:1", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}]},
            {"turn_key": "turn:2", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}]},
        ],
        "diagnostics": {"orphan_turn_keys": []},
    }
    failures, _, _ = evaluate_alignment(session, manifest, {"nonce": "nonce", "turn_key": "turn:1"}, tmp_path)
    assert any(issue["code"] == "ARTIFACT_MULTI_TURN_OWNER" for issue in failures)
