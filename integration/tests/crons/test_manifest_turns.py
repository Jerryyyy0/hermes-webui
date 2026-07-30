"""Cron-specific manifest turn normalization tests."""

from __future__ import annotations

from types import SimpleNamespace

from integration.crons.hooks import (
    _MAX_ITERATION_SUMMARY_REQUEST,
    _persist_cron_turn_artifacts,
    normalize_cron_manifest_messages,
    prepare_cron_session_for_reply,
)


def _tool_trace(*, user_content: str = "build report") -> list[dict]:
    return [
        {"role": "user", "content": user_content, "timestamp": 10.0},
        {
            "role": "assistant",
            "content": "",
            "timestamp": 20.0,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"report.html"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok", "timestamp": 30.0},
        {"role": "user", "content": _MAX_ITERATION_SUMMARY_REQUEST, "timestamp": 40.0},
        {"role": "assistant", "content": "Created `report.html`.", "timestamp": 50.0},
    ]


def test_normalize_cron_manifest_messages_removes_internal_turn_boundary():
    messages = normalize_cron_manifest_messages(_tool_trace())

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[-1]["content"] == "Created `report.html`."


def test_normalize_cron_manifest_messages_preserves_real_matching_user_text():
    messages = [
        {"role": "user", "content": _MAX_ITERATION_SUMMARY_REQUEST},
        {"role": "assistant", "content": "That is the configured limit message."},
    ]

    assert normalize_cron_manifest_messages(messages) == messages


def test_normalize_cron_manifest_messages_preserves_historical_stamped_internal_turn():
    messages = _tool_trace()
    messages[0]["_turn_key"] = "turn:1"
    messages[3]["_turn_key"] = "turn:47"

    assert normalize_cron_manifest_messages(
        messages,
        require_stable_real_turn=True,
    ) == messages


def test_manifest_get_reuses_normalized_cron_view(monkeypatch):
    import api.models as models
    from api.session_manifest import _load_display_messages

    merged = _tool_trace()
    merged[0]["_turn_key"] = "turn:1"
    session = SimpleNamespace(
        session_id="cron_job_20260713_120000",
        source_tag="cron",
        profile="abc",
        messages=list(merged),
        truncation_watermark=None,
    )
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        models,
        "merge_session_messages_append_only",
        lambda *args, **kwargs: list(merged),
    )

    messages = _load_display_messages(session)

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert messages[0]["_turn_key"] == "turn:1"
    assert messages[-1]["content"] == "Created `report.html`."


def test_manifest_get_does_not_change_non_cron_messages(monkeypatch):
    import api.models as models
    from api.session_manifest import _load_display_messages

    merged = _tool_trace()
    session = SimpleNamespace(
        session_id="webui-session",
        source_tag="webui",
        profile=None,
        messages=list(merged),
        truncation_watermark=None,
    )
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        models,
        "merge_session_messages_append_only",
        lambda *args, **kwargs: list(merged),
    )

    assert _load_display_messages(session) == merged


def test_normalized_cron_artifact_stays_on_real_turn(tmp_path):
    from api.models import Session
    from api.session_manifest import (
        _message_turns,
        extract_turn_artifact_entries_for_manifest,
    )
    from integration.crons.hooks import _stamp_cron_manifest_turn_keys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.html").write_text("<h1>report</h1>", encoding="utf-8")
    session = Session(
        session_id="cron_job_20260713_120003",
        source_tag="cron",
        workspace=str(workspace),
        messages=_stamp_cron_manifest_turn_keys(
            normalize_cron_manifest_messages(_tool_trace())
        ),
    )

    turns = _message_turns(session.messages)
    entries = extract_turn_artifact_entries_for_manifest(session, "turn:1")

    assert [turn["turn_key"] for turn in turns] == ["turn:1"]
    assert entries == [
        {
            "path": "report.html",
            "source_tool": "write_file",
            "preview": "file",
        }
    ]


def test_cron_manifest_store_artifact_aligns_with_real_turn(tmp_path, monkeypatch):
    import api.models as models
    import api.session_manifest_store as manifest_store
    from api.models import Session
    from api.session_manifest import build_session_manifest
    from api.session_manifest_store import upsert_manifest_records
    from integration.crons.hooks import _stamp_cron_manifest_turn_keys

    monkeypatch.setattr(manifest_store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *args, **kwargs: [])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.html").write_text("<h1>report</h1>", encoding="utf-8")
    session = Session(
        session_id="cron_job_20260713_120004",
        source_tag="cron",
        workspace=str(workspace),
        messages=_stamp_cron_manifest_turn_keys(
            normalize_cron_manifest_messages(_tool_trace())
        ),
    )
    upsert_manifest_records(
        session,
        "turn:1",
        [{"path": "report.html", "source_tool": "write_file", "preview": "file"}],
    )

    manifest = build_session_manifest(session)

    assert [turn["turn_key"] for turn in manifest["turns"]] == ["turn:1"]
    assert manifest["artifacts"] == manifest["turns"][0]["artifacts"]
    assert manifest["artifacts"][0]["path"] == "report.html"


def test_prepare_cron_session_for_reply_stamps_prefix_before_followup(monkeypatch):
    import api.session_manifest_store as manifest_store
    import api.streaming as streaming

    persisted: list[str] = []
    session = SimpleNamespace(
        session_id="cron_job_20260713_120099",
        source_tag="cron",
        profile="default",
        cron_execution_ended_at=100.0,
        messages=[
            *_tool_trace(),
            {"role": "user", "content": "follow up", "timestamp": 150.0, "_turn_key": "turn:2"},
            {"role": "assistant", "content": "follow-up answer", "timestamp": 160.0},
        ],
        save=lambda **_kwargs: None,
    )
    monkeypatch.setattr(manifest_store, "load_manifest_decided_turn_keys", lambda _session: set())
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda _session, turn_key: persisted.append(turn_key) or {
            "status": "persisted", "turn_key": turn_key,
        },
    )

    prepared = prepare_cron_session_for_reply(session)

    assert prepared.ready is True
    assert prepared.next_turn_key == "turn:3"
    assert persisted == ["turn:1"]
    assert [
        message.get("_turn_key")
        for message in session.messages
        if message.get("role") == "user"
    ] == ["turn:1", "turn:2"]


def test_prepare_cron_session_for_reply_backfills_legacy_execution_boundary(monkeypatch):
    import api.session_manifest_store as manifest_store
    import api.streaming as streaming
    import integration.crons.hooks as hooks

    saved = []
    session = SimpleNamespace(
        session_id="cron_job_20260713_120099",
        source_tag="cron",
        profile="default",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
        messages=_tool_trace(),
        save=lambda **kwargs: saved.append(kwargs),
    )
    monkeypatch.setattr(hooks, "resolve_cron_execution_ended_at", lambda _session: 100.0)
    monkeypatch.setattr(manifest_store, "load_manifest_decided_turn_keys", lambda _session: set())
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda _session, turn_key: {"status": "persisted", "turn_key": turn_key},
    )

    prepared = prepare_cron_session_for_reply(session)

    assert prepared.ready is True
    assert prepared.next_turn_key == "turn:2"
    assert session.cron_execution_ended_at == 100.0
    assert saved == [{"touch_updated_at": False}, {"touch_updated_at": False}]


def test_prepare_cron_session_for_reply_remains_closed_without_authoritative_boundary(monkeypatch):
    import integration.crons.hooks as hooks

    session = SimpleNamespace(
        session_id="cron_job_20260713_120099",
        source_tag="cron",
        profile="default",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
        messages=_tool_trace(),
    )
    monkeypatch.setattr(hooks, "resolve_cron_execution_ended_at", lambda _session: None)

    prepared = prepare_cron_session_for_reply(session)

    assert prepared.ready is False
    assert prepared.error_stage == "execution_prefix"


def test_prepare_cron_session_for_reply_preserves_followup_suffix_after_backfill(monkeypatch):
    import api.session_manifest_store as manifest_store
    import api.streaming as streaming
    import integration.crons.hooks as hooks

    session = SimpleNamespace(
        session_id="cron_job_20260713_120099",
        source_tag="cron",
        profile="default",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
        messages=[
            *_tool_trace(),
            {"role": "user", "content": "follow up", "timestamp": 150.0, "_turn_key": "turn:2"},
            {"role": "assistant", "content": "follow-up answer", "timestamp": 160.0},
        ],
        save=lambda **_kwargs: None,
    )
    monkeypatch.setattr(hooks, "resolve_cron_execution_ended_at", lambda _session: 100.0)
    monkeypatch.setattr(manifest_store, "load_manifest_decided_turn_keys", lambda _session: set())
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda _session, turn_key: {"status": "persisted", "turn_key": turn_key},
    )

    prepared = prepare_cron_session_for_reply(session)

    assert prepared.ready is True
    assert prepared.next_turn_key == "turn:3"
    assert [message["content"] for message in session.messages[-2:]] == [
        "follow up",
        "follow-up answer",
    ]
    assert [
        message.get("_turn_key")
        for message in session.messages
        if message.get("role") == "user"
    ] == ["turn:1", "turn:2"]


def test_prepare_cron_session_for_reply_fails_when_boundary_backfill_cannot_save(monkeypatch):
    import integration.crons.hooks as hooks

    session = SimpleNamespace(
        session_id="cron_job_20260713_120099",
        source_tag="cron",
        profile="default",
        cron_execution_profile="execution",
        cron_execution_ended_at=None,
        messages=_tool_trace(),
        save=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(hooks, "resolve_cron_execution_ended_at", lambda _session: 100.0)

    prepared = prepare_cron_session_for_reply(session)

    assert prepared.ready is False
    assert prepared.error_stage == "save"


def test_persist_cron_turn_artifacts_stamps_one_real_turn_before_decision(monkeypatch):
    import api.models as models
    import api.streaming as streaming

    events: list[tuple] = []
    session = SimpleNamespace(
        session_id="cron_job_20260713_120000",
        messages=_tool_trace(),
        cron_execution_ended_at=100.0,
        save=lambda: events.append(("save",)),
    )
    monkeypatch.setattr(models.Session, "load", lambda sid: session)
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda current, turn_key: events.append(("persist", turn_key, list(current.messages))),
    )

    _persist_cron_turn_artifacts(session.session_id)

    assert events[0] == ("save",)
    assert events[1][0:2] == ("persist", "turn:1")
    assert [message["role"] for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert session.messages[0]["_turn_key"] == "turn:1"
    assert events[1][2][-1]["content"] == "Created `report.html`."


def test_persist_cron_turn_artifacts_keeps_real_turns_contiguous(monkeypatch):
    import api.models as models
    import api.streaming as streaming
    import api.session_manifest_store as manifest_store

    monkeypatch.setattr(manifest_store, "load_manifest_decided_turn_keys", lambda _session: set())
    messages = _tool_trace()
    messages.extend(
        [
            {"role": "user", "content": "second real request", "timestamp": 60.0},
            {"role": "assistant", "content": "second answer", "timestamp": 70.0},
        ]
    )
    persisted: list[str] = []
    session = SimpleNamespace(messages=messages, cron_execution_ended_at=100.0, save=lambda: None)
    monkeypatch.setattr(models.Session, "load", lambda sid: session)
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda current, turn_key: persisted.append(turn_key),
    )

    _persist_cron_turn_artifacts("cron_job_20260713_120001")

    assert persisted == ["turn:1", "turn:2"]
    assert [
        message.get("_turn_key")
        for message in session.messages
        if message.get("role") == "user"
    ] == ["turn:1", "turn:2"]


def test_persist_cron_turn_artifacts_ignores_followup_suffix(monkeypatch):
    import api.models as models
    import api.streaming as streaming

    messages = _tool_trace()
    messages.extend(
        [
            {"role": "user", "content": "follow up", "timestamp": 150.0, "_turn_key": "turn:2"},
            {"role": "assistant", "content": "follow-up answer", "timestamp": 160.0},
        ]
    )
    persisted: list[str] = []
    session = SimpleNamespace(
        messages=messages,
        cron_execution_ended_at=100.0,
        save=lambda: None,
    )
    monkeypatch.setattr(models.Session, "load", lambda sid: session)
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda current, turn_key: persisted.append(turn_key),
    )

    _persist_cron_turn_artifacts("cron_job_20260713_120003")

    assert persisted == ["turn:1"]
    assert [
        message.get("_turn_key")
        for message in session.messages
        if isinstance(message, dict) and message.get("role") == "user"
    ] == ["turn:1", "turn:2"]
    assert [message["content"] for message in session.messages if message.get("role") == "user"] == [
        "build report",
        "follow up",
    ]


def test_persist_cron_turn_artifacts_does_not_write_decision_when_save_fails(monkeypatch):
    import api.models as models
    import api.streaming as streaming

    def fail_save():
        raise OSError("disk full")

    session = SimpleNamespace(
        messages=_tool_trace(),
        cron_execution_ended_at=100.0,
        save=fail_save,
    )
    persisted: list[str] = []
    monkeypatch.setattr(models.Session, "load", lambda sid: session)
    monkeypatch.setattr(
        streaming,
        "_persist_turn_artifact_paths",
        lambda current, turn_key: persisted.append(turn_key),
    )

    _persist_cron_turn_artifacts("cron_job_20260713_120002")

    assert persisted == []
