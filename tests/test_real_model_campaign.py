import json
import random
import sqlite3

from scripts.real_model_campaign import (
    HistoryPrompt,
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
    pick_history_prompt,
    pool_for_context_mode,
    resolve_batch_specs,
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


def test_resolve_batch_specs_adds_cancel_verify_when_sessions_ge_2():
    assert [spec["kind"] for spec in resolve_batch_specs(1, 5)] == ["normal"]
    specs = resolve_batch_specs(2, 5)
    assert [spec["kind"] for spec in specs] == ["normal", "normal", "cancel_verify"]
    assert specs[-1]["batch_index"] == 3
    assert specs[-1]["cancel_plan"] == immediate_cancel_plan(5)
    assert [spec["kind"] for spec in resolve_batch_specs(2, 5, cancel_verify_session=False)] == [
        "normal",
        "normal",
    ]


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
