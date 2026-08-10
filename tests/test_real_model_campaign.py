import argparse
import json
import random
import sqlite3
import sys
import types
from contextlib import nullcontext

import pytest

from scripts.real_model_campaign import (
    CANCEL_TRIGGERS,
    HistoryPrompt,
    _call_llm_accepts_api_mode,
    _model_prompt_generation_error,
    _generated_prompt_texts,
    _run_round,
    _wait_for_chat_ready,
    build_history_prompt,
    cancellation_plan,
    cleanup_campaign_test_data,
    create_plain_session,
    drain_blocking_prompts,
    enable_auto_approve,
    evaluate_alignment,
    generate_model_prompt_pool,
    list_campaign_test_sessions,
    load_history_prompt_pool,
    load_prefix_messages,
    model_campaign_phase,
    parse_bool_arg,
    pick_history_prompt,
    pool_for_context_mode,
    _persist_campaign_summary,
    random_cancel_verify_plan,
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


class _PlainSessionApi:
    def __init__(self, workspace):
        self.calls = []
        self.workspace = workspace

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if path == "/api/session/new":
            return {"session": {"session_id": "sid-1", "workspace": str(self.workspace)}}
        if path == "/api/session/rename":
            return {"ok": True}
        if path == "/api/session/yolo":
            return {"ok": True}
        raise AssertionError(f"unexpected request: {method} {path}")


def test_default_cancel_plan_covers_all_paths():
    assert cancellation_plan(15) == {
        3: "immediate_after_start",
        6: "after_first_tool",
        8: "after_manifest_delta",
        12: "after_first_artifact",
    }


def test_random_cancel_verify_plan_cancels_every_turn_with_known_triggers():
    plan = random_cancel_verify_plan(8, random.Random(42))
    assert set(plan) == set(range(1, 9))
    assert all(trigger in CANCEL_TRIGGERS for trigger in plan.values())
    assert plan == random_cancel_verify_plan(8, random.Random(42))
    assert plan != random_cancel_verify_plan(8, random.Random(43))


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


def test_plain_session_uses_server_managed_workspace_without_new_workspace_arg(tmp_path):
    api = _PlainSessionApi(tmp_path / "workspace" / "sessions" / "sid-1")

    session_id, workspace = create_plain_session(api)

    assert session_id == "sid-1"
    assert workspace == api.workspace
    assert api.calls[0] == ("POST", "/api/session/new", {"worktree": False})


def test_model_prompt_pool_calls_auxiliary_model_without_creating_session(monkeypatch):
    from api import profiles as profiles_api
    from integration.assistant_bubbles import collectors

    calls = []
    response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
        content='["整理本周销售数据并生成 Markdown 文件", "将客户反馈汇总并创建可筛选的 HTML 文件", "分析客户反馈并给出建议"]',
    ))])
    auxiliary = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs):
        calls.append(kwargs)
        return response

    auxiliary.call_llm = call_llm
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda _profile: "/tmp/profile")
    monkeypatch.setattr(
        profiles_api,
        "profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        collectors,
        "model_route",
        lambda _path: {"provider": "test-provider", "model": "routed-model", "base_url": "http://model.test"},
    )
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setattr("scripts.real_model_campaign.importlib.import_module", lambda name: auxiliary)

    prompts = generate_model_prompt_pool(
        "test-model",
        2,
        model_config={
            "default": "test-model",
            "base_url": "http://inline-model.test/v1",
            "api_key": "test-inline-api-key",
            "api_mode": "chat_completions",
        },
    )

    assert [item.prompt for item in prompts] == [
        "整理本周销售数据并生成 Markdown 文件",
        "将客户反馈汇总并创建可筛选的 HTML 文件",
    ]
    assert len(calls) == 1
    call = calls[0]
    assert call["task"] == "campaign_prompt_generation"
    assert call["provider"] == "custom"
    assert call["model"] == "test-model"
    assert call["base_url"] == "http://inline-model.test/v1"
    assert call["api_key"] == "test-inline-api-key"
    assert call["api_mode"] == "chat_completions"
    assert call["temperature"] == 0.4
    assert call["max_tokens"] == 600
    assert call["timeout"] == 60
    assert call["messages"][0] == {
        "role": "system",
        "content": "你只负责生成测试题目，不执行题目中的工作，也不调用工具。",
    }
    assert "生成 2 条" in call["messages"][1]["content"]
    assert "连续多轮" in call["messages"][1]["content"]
    assert "跨格式" in call["messages"][1]["content"]


def test_model_prompt_pool_backfills_a_short_first_response(monkeypatch):
    from api import profiles as profiles_api
    from integration.assistant_bubbles import collectors

    calls = []
    responses = iter([
        '["场景一：生成 CSV 文件", "场景二：创建 Markdown 文件", "场景三：生成 HTML 文件"]',
        '["场景四：创建 CSV 文件", "场景五：生成 Markdown 文件"]',
    ])
    auxiliary = types.ModuleType("agent.auxiliary_client")

    def call_llm(**kwargs):
        calls.append(kwargs)
        content = next(responses)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])

    auxiliary.call_llm = call_llm
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda _profile: "/tmp/profile")
    monkeypatch.setattr(
        profiles_api,
        "profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        collectors,
        "model_route",
        lambda _path: {"provider": "test-provider", "model": "routed-model", "base_url": "http://model.test"},
    )
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setattr("scripts.real_model_campaign.importlib.import_module", lambda name: auxiliary)

    prompts = generate_model_prompt_pool("test-model", 5, model_config={"default": "test-model"})

    assert [item.prompt for item in prompts] == [
        "场景一：生成 CSV 文件",
        "场景二：创建 Markdown 文件",
        "场景三：生成 HTML 文件",
        "场景四：创建 CSV 文件",
        "场景五：生成 Markdown 文件",
    ]
    assert len(calls) == 2
    assert "生成 5 条" in calls[0]["messages"][1]["content"]
    assert "生成 2 条" in calls[1]["messages"][1]["content"]


def test_model_prompt_parser_rejects_requests_without_explicit_file_generation():
    prompts = _generated_prompt_texts(
        '["请把销售数据写入一个 Markdown 文件", "请汇总销售数据并生成 CSV 文件"]'
    )

    assert prompts == ["请汇总销售数据并生成 CSV 文件"]


def test_model_prompt_pool_falls_back_to_bubble_route_without_inline_endpoint(monkeypatch):
    from api import profiles as profiles_api
    from integration.assistant_bubbles import collectors

    calls = []
    response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
        content='["请汇总销售数据并生成 CSV 文件"]',
    ))])
    auxiliary = types.ModuleType("agent.auxiliary_client")
    auxiliary.call_llm = lambda **kwargs: calls.append(kwargs) or response
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda _profile: "/tmp/profile")
    monkeypatch.setattr(
        profiles_api,
        "profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        collectors,
        "model_route",
        lambda _path: {"provider": "test-provider", "model": "routed-model", "base_url": "http://model.test"},
    )
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setattr("scripts.real_model_campaign.importlib.import_module", lambda name: auxiliary)

    generate_model_prompt_pool("test-model", 1, model_config={"default": "test-model"})

    assert calls[0]["provider"] == "test-provider"
    assert calls[0]["model"] == "test-model"
    assert calls[0]["base_url"] == "http://model.test"
    assert calls[0]["api_key"] is None
    assert "api_mode" not in calls[0]


def test_model_prompt_pool_redacts_upstream_auth_error(monkeypatch):
    from api import profiles as profiles_api
    from integration.assistant_bubbles import collectors

    auxiliary = types.ModuleType("agent.auxiliary_client")

    def call_llm(**_kwargs):
        raise RuntimeError("authentication failed for test-secret")

    auxiliary.call_llm = call_llm
    monkeypatch.setattr(profiles_api, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda _profile: "/tmp/profile")
    monkeypatch.setattr(
        profiles_api,
        "profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        collectors,
        "model_route",
        lambda _path: {"provider": "test-provider", "model": "test-model", "base_url": "http://model.test"},
    )
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    monkeypatch.setattr("scripts.real_model_campaign.importlib.import_module", lambda name: auxiliary)

    with pytest.raises(RuntimeError, match="check the current profile's model endpoint and credentials") as exc_info:
        generate_model_prompt_pool("test-model", 1, model_config={"default": "test-model"})

    assert "test-secret" not in str(exc_info.value)


def test_model_prompt_generation_error_classifies_http_status_without_upstream_text():
    class _UpstreamError(Exception):
        status_code = 401

        def __str__(self):
            return "invalid key test-secret at https://provider.example/v1"

    message = _model_prompt_generation_error(_UpstreamError())

    assert message == "model prompt generation authentication failed (HTTP 401); check the current profile's model credentials"
    assert "test-secret" not in message
    assert "provider.example" not in message


def test_call_llm_api_mode_compatibility_detection():
    def legacy_call_llm(*, messages):
        return messages

    def current_call_llm(*, messages, api_mode=None):
        return messages, api_mode

    def flexible_call_llm(**kwargs):
        return kwargs

    assert _call_llm_accepts_api_mode(legacy_call_llm) is False
    assert _call_llm_accepts_api_mode(current_call_llm) is True
    assert _call_llm_accepts_api_mode(flexible_call_llm) is True


def test_model_prompt_source_skips_history_database(tmp_path, monkeypatch):
    question = HistoryPrompt("generated", "model-generated", "生成一份 Markdown 报告", 1, "generated:1", 0, 0, ())
    api = _CancelFlowApi(
        stream_events=[],
        workspace=tmp_path / "workspace" / "sessions" / "sid-1",
        health={"status": "ok", "active_streams": ["other-session"], "active_runs": ["other-run"]},
    )
    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: api)
    monkeypatch.setattr("scripts.real_model_campaign._state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "scripts.real_model_campaign.load_history_prompt_pool",
        lambda _state_dir: pytest.fail("model prompt source must not read the history database"),
    )
    monkeypatch.setattr(
        "scripts.real_model_campaign.generate_model_prompt_pool",
        lambda _model, _count, **_kwargs: [question],
    )
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())
    monkeypatch.setattr(
        "scripts.real_model_campaign.random_cancel_verify_plan",
        lambda turns, _rng: {turn: "immediate_after_start" for turn in range(1, turns + 1)},
    )

    assert run_campaign(
        1,
        3,
        "http://test",
        context_mode="first",
        cancel_verify_session=True,
        prompt_source="model",
    ) == 0


def test_model_prompt_source_generates_one_scenario_per_session(monkeypatch):
    class _StopGeneration(Exception):
        pass

    class _HealthyApi:
        def request(self, method, path, body=None, timeout=20):
            assert (method, path) == ("GET", "/health")
            return {"status": "ok", "active_streams": [], "active_runs": []}

    counts = []

    def stop_after_recording(_model, count, **_kwargs):
        counts.append(count)
        raise _StopGeneration()

    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: _HealthyApi())
    monkeypatch.setattr("scripts.real_model_campaign.generate_model_prompt_pool", stop_after_recording)

    with pytest.raises(_StopGeneration):
        run_campaign(2, 3, "http://test", prompt_source="model")

    assert counts == [2]


def test_model_prompt_source_reuses_one_scenario_for_every_turn_in_a_batch(tmp_path, monkeypatch):
    class _HealthyApi:
        def request(self, method, path, body=None, timeout=20):
            assert (method, path) == ("GET", "/health")
            return {"status": "ok", "active_streams": [], "active_runs": []}

    scenarios = [
        HistoryPrompt("model-generated", "a", "场景 A：生成 CSV 文件", 1, "generated:1", 0, 0, ()),
        HistoryPrompt("model-generated", "b", "场景 B：生成 HTML 文件", 1, "generated:2", 0, 0, ()),
    ]
    recorded = []
    session_counter = iter(range(1, 3))

    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: _HealthyApi())
    monkeypatch.setattr("scripts.real_model_campaign._state_dir", lambda: tmp_path)
    monkeypatch.setattr("scripts.real_model_campaign.generate_model_prompt_pool", lambda *_args, **_kwargs: scenarios)
    monkeypatch.setattr(
        "scripts.real_model_campaign.create_plain_session",
        lambda _api: (f"sid-{next(session_counter)}", tmp_path / f"workspace-{len(recorded)}"),
    )

    def record_round(_api, session_id, _workspace, question, _campaign_id, turn, _trigger, **kwargs):
        recorded.append((session_id, turn, question.prompt, kwargs["model_campaign"]))
        return {"turn": turn, "alignment_failures": [], "observations": []}

    monkeypatch.setattr("scripts.real_model_campaign._run_round", record_round)

    assert run_campaign(2, 3, "http://test", prompt_source="model", cancel_verify_session=False, seed=7) == 0

    assert [row[3] for row in recorded] == [True] * 6
    first_batch = [row[2] for row in recorded[:3]]
    second_batch = [row[2] for row in recorded[3:]]
    assert len(set(first_batch)) == len(set(second_batch)) == 1
    assert first_batch[0] != second_batch[0]


def test_campaign_allows_busy_webui_by_default(monkeypatch):
    class _BusyApi:
        def request(self, method, path, body=None, timeout=20):
            assert (method, path) == ("GET", "/health")
            return {"status": "ok", "active_streams": ["other-session"], "active_runs": []}

    class _StopAfterHealth(Exception):
        pass

    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: _BusyApi())
    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr(
        "scripts.real_model_campaign.generate_model_prompt_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_StopAfterHealth()),
    )

    with pytest.raises(_StopAfterHealth):
        run_campaign(1, 3, "http://test", prompt_source="model")


def test_campaign_can_still_require_an_idle_webui(monkeypatch):
    class _BusyApi:
        def request(self, method, path, body=None, timeout=20):
            assert (method, path) == ("GET", "/health")
            return {"status": "ok", "active_streams": ["other-session"], "active_runs": []}

    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: _BusyApi())

    with pytest.raises(RuntimeError, match="active streams or runs"):
        run_campaign(1, 5, "http://test", allow_concurrent=False)


def test_manifest_integration_prefix_resolves_inside_session_workspace(tmp_path, monkeypatch):
    integration_root = tmp_path / "workspace"
    session_workspace = integration_root / "sessions" / "sid-1"
    artifact = session_workspace / "data" / "source.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("id,value\n1,ok\n", encoding="utf-8")
    monkeypatch.setattr("api.workspace.resolve_trusted_workspace", lambda _profile=None: integration_root)

    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [{
            "turn_key": "turn:1",
            "artifacts": [{"path": "sessions/sid-1/data/source.csv"}],
        }],
        "diagnostics": {"orphan_turn_keys": []},
    }

    failures, observations, hashes = evaluate_alignment(
        session, manifest, {"nonce": "nonce"}, session_workspace,
    )

    assert failures == []
    assert observations == []
    assert "sessions/sid-1/data/source.csv" in hashes


def test_nonce_only_checks_current_expected_delivery(tmp_path):
    current = tmp_path / "deliverables/turn-04/delivery.md"
    previous = tmp_path / "deliverables/turn-03/delivery.md"
    current.parent.mkdir(parents=True)
    current.write_text("NONCE=current\n", encoding="utf-8")
    previous.parent.mkdir(parents=True)
    previous.write_text("NONCE=previous\n", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "current", "_turn_key": "turn:4"}]}
    manifest = {
        "turns": [{
            "turn_key": "turn:4",
            "artifacts": [
                {"path": "deliverables/turn-03/delivery.md"},
                {"path": "deliverables/turn-04/delivery.md"},
            ],
        }],
        "diagnostics": {"orphan_turn_keys": []},
    }

    failures, observations, _ = evaluate_alignment(
        session,
        manifest,
        {"nonce": "current", "expected_artifact_path": "deliverables/turn-04/delivery.md"},
        tmp_path,
    )

    assert failures == []
    assert observations == []


def test_campaign_summary_persists_partial_and_complete_states(tmp_path):
    path = tmp_path / "campaign.json"
    summary = {"campaign_id": "camp-1", "batches": []}

    _persist_campaign_summary(path, summary, completed=False)
    assert json.loads(path.read_text(encoding="utf-8"))["completed"] is False
    _persist_campaign_summary(path, summary, completed=True)
    assert json.loads(path.read_text(encoding="utf-8"))["completed"] is True


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

    def __init__(self, stream_events, workspace=None, health=None):
        self.calls = []
        self.stream_events = list(stream_events)
        self.workspace = workspace
        self.health = health or {"status": "ok", "active_streams": [], "active_runs": []}
        self.events_opened = 0
        self.stream_connections = []

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path, body))
        if path == "/health":
            return self.health
        if path == "/api/session/new":
            return {"session": {"session_id": "sid-1", "workspace": str(self.workspace or "")}}
        if path == "/api/session/rename":
            return {"ok": True}
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
    monkeypatch.setattr(
        "scripts.real_model_campaign.random_cancel_verify_plan",
        lambda turns, _rng: {turn: "immediate_after_start" for turn in range(1, turns + 1)},
    )

    api = _CancelFlowApi(stream_events=[], workspace=tmp_path / "workspace" / "sessions" / "sid-1")
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
    assert not any(path.startswith("/api/session/manifest") for path in paths)
    chat_start_bodies = [body for _method, path, body in api.calls if path == "/api/chat/start"]
    assert chat_start_bodies
    assert all(body["workspace"] == str(api.workspace) for body in chat_start_bodies)


def test_campaign_returns_failure_when_normal_turn_has_no_artifact(tmp_path, monkeypatch):
    api = _CancelFlowApi(stream_events=[], workspace=tmp_path / "workspace" / "sessions" / "sid-1")
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))
    monkeypatch.setattr("api.config.get_config", lambda: {"model": {"default": "test-model"}})
    monkeypatch.setattr("scripts.real_model_campaign.Api", lambda _base_url: api)
    monkeypatch.setattr("scripts.real_model_campaign._state_dir", lambda: tmp_path)
    monkeypatch.setattr("scripts.real_model_campaign.load_history_prompt_pool", lambda _state_dir: [question])
    monkeypatch.setattr("scripts.real_model_campaign.pool_for_context_mode", lambda pool, _mode: pool)
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())

    assert run_campaign(1, 5, "http://test", context_mode="first", cancel_verify_session=False) == 1


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


def test_successful_normal_cancel_skips_artifact_alignment(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.real_model_campaign.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())
    api = _CancelFlowApi(stream_events=[("tool", {"name": "write_file"})], workspace=tmp_path)
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))

    row = _run_round(api, "sid-1", tmp_path, question, "camp-1", 1, "after_first_tool")

    assert row["cancel"]["cancelled"] is True
    assert {item["code"] for item in row["observations"]} >= {"ARTIFACT_ALIGNMENT_SKIPPED"}
    assert not any(path.startswith("/api/session/manifest") for _method, path, _body in api.calls)


def test_stream_error_is_recorded_without_upstream_error_details(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.real_model_campaign.uuid.uuid4", lambda: type("U", (), {"hex": "nonce-marker"})())
    api = _CancelFlowApi(
        stream_events=[("apperror", {"type": "provider_error", "error_code": "model_unavailable", "details": "secret"})],
        workspace=tmp_path,
    )
    question = HistoryPrompt("s1", "t", "生成报告", 1, "turn:0", 6, 1, ("a.md",))

    row = _run_round(api, "sid-1", tmp_path, question, "camp-1", 1, None)

    errors = [item for item in row["observations"] if item["code"] == "MODEL_STREAM_ERROR"]
    assert errors == [{"code": "MODEL_STREAM_ERROR", "error_code": "model_unavailable", "type": "provider_error"}]
    assert "secret" not in str(row["observations"])


def test_resolve_batch_specs_cancel_verify_is_first_session_when_enabled():
    assert [spec["kind"] for spec in resolve_batch_specs(2, 5)] == ["normal", "normal"]
    specs = resolve_batch_specs(2, 5, cancel_verify_session=True, rng=random.Random(7))
    assert [spec["kind"] for spec in specs] == ["cancel_verify", "normal"]
    assert specs[0]["batch_index"] == 1
    assert specs[0]["cancel_plan"] == random_cancel_verify_plan(5, random.Random(7))
    assert set(specs[0]["cancel_plan"].values()) <= set(CANCEL_TRIGGERS)
    assert len(specs[0]["cancel_plan"]) == 5
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


def test_model_campaign_prompt_uses_progressive_business_files_without_delivery_wrapper():
    question = HistoryPrompt(
        source_session_id="model-generated",
        title="model-generated",
        prompt="为门店经营复盘生成文件，处理异常值并说明口径。",
        turn=1,
        turn_key="generated:1",
        n_tools=0,
        n_write_tools=0,
        artifact_paths=(),
    )

    first_prompt, first_expected = build_history_prompt(question, 1, "nonce-1", model_campaign=True)
    fourth_prompt, fourth_expected = build_history_prompt(question, 4, "nonce-4", model_campaign=True)

    assert first_expected == ""
    assert fourth_expected == ""
    assert "data/source.csv" in first_prompt
    assert "review/iteration-04.md" in fourth_prompt
    assert "NONCE=nonce-1" in first_prompt
    assert "deliverables/turn-01/delivery.md" not in first_prompt


def test_model_campaign_phase_cycles_after_the_core_three_turns():
    assert model_campaign_phase(1)[0] == "data/source.csv"
    assert model_campaign_phase(2)[0] == "report/analysis.md"
    assert model_campaign_phase(3)[0] == "dashboard/index.html"
    assert model_campaign_phase(4)[0] == "review/iteration-04.md"
    assert model_campaign_phase(5)[0] == "data/derived-05.csv"
    assert model_campaign_phase(6)[0] == "dashboard/iteration-06.html"
    assert model_campaign_phase(7)[0] == "review/iteration-07.md"


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


def _write_tools(ids_prefix: str, *, count: int = 10, duplicate_ids: bool = False):
    names = ("terminal", "read_file", "search_files", "todo", "write_file")
    return [
        {
            "id": f"{ids_prefix}{1 if duplicate_ids else i}",
            "type": "function",
            "function": {"name": names[(i - 1) % len(names)], "arguments": "{}"},
        }
        for i in range(1, count + 1)
    ]


def test_load_history_prompt_pool_keeps_first_and_mid_turns(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    sid = "hist001abc"
    messages = [
        {"role": "user", "content": "分析日志并生成可视化 html 报告", "_turn_key": "turn:0"},
        # All ten events carry the same ID to prove history selection counts
        # calls as recorded rather than deduplicating tool_call_id values.
        {"role": "assistant", "content": "", "tool_calls": _write_tools("c", duplicate_ids=True)},
        {"role": "tool", "tool_call_id": "c5", "name": "write_file", "content": "ok"},
        {"role": "user", "content": "再给我一个 word 版本", "_turn_key": "turn:1"},
        {"role": "assistant", "content": "", "tool_calls": _write_tools("d")},
        {"role": "tool", "tool_call_id": "d5", "name": "write_file", "content": "ok"},
        {"role": "user", "content": "再生成一个 PDF 交付版本", "_turn_key": "turn:2"},
        {"role": "assistant", "content": "", "tool_calls": _write_tools("e", count=9)},
        {"role": "tool", "tool_call_id": "e5", "name": "write_file", "content": "ok"},
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
        [
            (sid, "turn:0", "report.html"),
            (sid, "turn:1", "report.docx"),
            (sid, "turn:2", "report.pdf"),
        ],
    )
    con.commit()
    con.close()

    pool = load_history_prompt_pool(tmp_path)
    assert {p.turn for p in pool} == {1, 2}
    assert {p.n_tools for p in pool} == {10}
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
    managed = tmp_path / "workspace" / "sessions" / "camp02"
    managed.mkdir(parents=True)
    (sessions / "camp02.json").write_text(
        json.dumps({
            "session_id": "camp02",
            "title": "campaign-first:camp02",
            "workspace": str(managed),
            "workspace_mode": "managed",
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

    assert [row["session_id"] for row in list_campaign_test_sessions(tmp_path)] == ["camp01", "camp02"]
    api = _FakeApi()
    result = cleanup_campaign_test_data(api, tmp_path)
    assert result["ok"] is True
    assert result["deleted_sessions"] == ["camp01", "camp02"]
    assert "20260806-demo" in result["removed_campaign_dirs"]
    assert result["removed_managed_workspaces"] == [str(managed)]
    assert api.calls[0][:2] == ("POST", "/api/session/delete")
    assert not (campaigns / "20260806-demo").exists()
    assert not managed.exists()
    assert (sessions / "keepme.json").is_file()


def test_cleanup_keeps_managed_workspace_when_session_delete_fails(tmp_path):
    managed = tmp_path / "workspace" / "sessions" / "camp02"
    managed.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "camp02.json").write_text(
        json.dumps({
            "session_id": "camp02",
            "title": "campaign-first:camp02",
            "workspace": str(managed),
            "workspace_mode": "managed",
            "messages": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    class _FailingDeleteApi:
        def request(self, method, path, body=None, timeout=20):
            assert (method, path, body) == ("POST", "/api/session/delete", {"session_id": "camp02"})
            return {"ok": False}

    result = cleanup_campaign_test_data(_FailingDeleteApi(), tmp_path)

    assert result["ok"] is False
    assert result["removed_managed_workspaces"] == []
    assert managed.is_dir()


def test_drain_blocking_prompts_resolves_approval_and_clarify():
    api = _FakeApi()
    counts = drain_blocking_prompts(api, "sid-1")
    assert counts == {"approvals": 1, "clarifies": 1}
    paths = [path for _method, path, _body in api.calls]
    assert "/api/approval/respond" in paths
    assert "/api/clarify/respond" in paths


def test_missing_artifact_is_an_alignment_failure(tmp_path):
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {"turns": [{"turn_key": "turn:1", "artifacts": []}], "diagnostics": {"orphan_turn_keys": []}}
    failures, observations, _ = evaluate_alignment(session, manifest, {"nonce": "nonce", "turn_key": "turn:1"}, tmp_path)
    assert failures == [{"code": "MODEL_NO_ARTIFACT"}]
    assert not observations


def test_skill_artifact_is_normal_when_turn_also_creates_a_workspace_file(tmp_path):
    delivered = tmp_path / "data/derived.csv"
    delivered.parent.mkdir(parents=True)
    delivered.write_text("id,value\n1,ok\n", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [{
            "turn_key": "turn:1",
            "artifacts": [
                {"path": "general/campaign-turn-delivery", "preview": "skill", "source_tool": "skill_manage"},
                {"path": "data/derived.csv", "preview": "file", "source_tool": "write_file"},
            ],
        }],
        "diagnostics": {"orphan_turn_keys": []},
    }

    failures, observations, hashes = evaluate_alignment(session, manifest, {"nonce": "nonce"}, tmp_path)

    assert failures == []
    assert observations == [{"code": "SKILL_ARTIFACT", "path": "general/campaign-turn-delivery"}]
    assert set(hashes) == {"data/derived.csv"}


def test_skill_artifact_does_not_satisfy_campaign_file_delivery(tmp_path):
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {
        "turns": [{
            "turn_key": "turn:1",
            "artifacts": [{"path": "general/campaign-turn-delivery", "source_tool": "skill_manage"}],
        }],
        "diagnostics": {"orphan_turn_keys": []},
    }

    failures, observations, _ = evaluate_alignment(session, manifest, {"nonce": "nonce"}, tmp_path)

    assert failures == [{"code": "MODEL_NO_ARTIFACT"}]
    assert observations == [{"code": "SKILL_ARTIFACT", "path": "general/campaign-turn-delivery"}]


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
