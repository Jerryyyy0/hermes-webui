"""Regression coverage for #2472 fork-from-here on messaging sessions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import api.routes as routes

REPO = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"async function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


def test_messaging_merge_helper_matches_session_get_coordinate_space():
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "sidecar only", "timestamp": 2},
            {"role": "assistant", "content": "shared", "timestamp": 3},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "cli earlier", "timestamp": 1},
        {"role": "assistant", "content": "shared", "timestamp": 3},
        {"role": "assistant", "content": "cli later", "timestamp": 4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == [
        "cli earlier",
        "sidecar only",
        "shared",
        "cli later",
    ]


def test_messaging_merge_helper_dedupes_equivalent_timestamp_formats():
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "hi", "timestamp": "10.0"},
            {"role": "assistant", "content": "same answer", "timestamp": "11.000000"},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "hi", "timestamp": 10},
        {"role": "assistant", "content": "same answer", "timestamp": 11},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == ["hi", "same answer"]


def test_messaging_merge_preserves_longer_sidecar_order_when_timestamps_collapse():
    """A repaired messaging sidecar can preserve order but lose subsecond timestamps.

    Re-sorting those messages by ``(timestamp, role, content)`` groups assistant
    and tool rows before user rows, making the WebUI look like replies vanished.
    """
    session = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "prior answer", "timestamp": 100.0},
            {"role": "user", "content": "first prompt", "timestamp": 101.0},
            {"role": "assistant", "content": "first answer", "timestamp": 101.0},
            {"role": "user", "content": "second prompt", "timestamp": 101.0},
            {"role": "assistant", "content": "second answer", "timestamp": 101.0},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "first prompt", "timestamp": 101.1},
        {"role": "assistant", "content": "first answer", "timestamp": 101.2},
        {"role": "user", "content": "second prompt", "timestamp": 101.3},
        {"role": "assistant", "content": "second answer", "timestamp": 101.4},
    ]

    merged = routes._merged_session_messages_for_display(session, cli_messages)

    assert [m["content"] for m in merged] == [
        "prior answer",
        "first prompt",
        "first answer",
        "second prompt",
        "second answer",
    ]


def test_messaging_weak_path_dedupes_recovered_vs_db_persisted_user():
    """Short sidecar + longer CLI must not keep recovered and state.db copies.

    Forces the chronological weak path (len(sidecar) < len(cli)). state.db rows
    carry ``id`` / ``_db_persisted`` while the sidecar recovery row uses a legacy
    merge key; merge-key-only dedupe previously left both visible.
    """
    bare = "我已上传 1 个文件: /home/hermeswebui/.hermes/webui/attachments/be83b6782a4d/image-1.png"
    composite = (
        f"{bare}\n\n[Workspace::v1: /workspace]\n{bare}"
    )
    session = SimpleNamespace(
        messages=[
            {
                "role": "user",
                "content": bare,
                "timestamp": 1785317767,
                "_recovered": True,
            },
            {
                "role": "assistant",
                "content": "**发生错误:** 模型服务返回错误，请稍后重试。",
                "timestamp": 1785317775,
                "_error": True,
            },
        ]
    )
    cli_messages = [
        {
            "role": "user",
            "content": composite,
            "_db_persisted": True,
            "id": 1,
        },
        {
            "role": "user",
            "content": bare,
            "_db_persisted": True,
            "id": 1,
        },
        {
            "role": "assistant",
            "content": "**发生错误:** 模型服务返回错误，请稍后重试。",
            "timestamp": 1785317775,
            "_error": True,
        },
        {
            "role": "user",
            "content": "follow-up only in cli",
            "timestamp": 1785317800,
            "_db_persisted": True,
            "id": 2,
        },
        {
            "role": "assistant",
            "content": "cli later reply",
            "timestamp": 1785317801,
        },
    ]
    assert len(session.messages) < len(cli_messages)

    merged = routes._merged_session_messages_for_display(session, cli_messages)
    user_contents = [m["content"] for m in merged if m.get("role") == "user"]

    assert user_contents.count(bare) + user_contents.count(composite) == 1
    assert "follow-up only in cli" in user_contents
    assert [m["content"] for m in merged if m.get("role") == "assistant"] == [
        "**发生错误:** 模型服务返回错误，请稍后重试。",
        "cli later reply",
    ]


def test_messaging_weak_path_keeps_intentional_duplicate_user_turns():
    """Identical user content twice must survive when both sources agree on count."""
    session = SimpleNamespace(
        messages=[
            {"role": "user", "content": "ok", "timestamp": 10},
            {"role": "assistant", "content": "first", "timestamp": 11},
            {"role": "user", "content": "ok", "timestamp": 12},
        ]
    )
    cli_messages = [
        {"role": "user", "content": "ok", "timestamp": 10, "id": 1, "_db_persisted": True},
        {"role": "assistant", "content": "first", "timestamp": 11},
        {"role": "user", "content": "ok", "timestamp": 12, "id": 2, "_db_persisted": True},
        {"role": "assistant", "content": "second", "timestamp": 13},
        {"role": "user", "content": "cli only", "timestamp": 14, "id": 3, "_db_persisted": True},
    ]
    assert len(session.messages) < len(cli_messages)

    merged = routes._merged_session_messages_for_display(session, cli_messages)
    assert [m["content"] for m in merged if m.get("role") == "user"] == [
        "ok",
        "ok",
        "cli only",
    ]


def test_branch_handler_uses_merged_messaging_messages_for_keep_count():
    branch_idx = ROUTES_PY.index('parsed.path == "/api/session/branch":')
    block = ROUTES_PY[branch_idx : branch_idx + 2600]

    assert "_merged_session_messages_for_display(source, cli_messages)" in block
    assert "get_cli_session_messages(source.session_id)" in block
    assert "source_messages = source.messages or []" not in block


def test_branch_handler_best_effort_saves_source_before_fork_slice():
    branch_idx = ROUTES_PY.index('parsed.path == "/api/session/branch":')
    block = ROUTES_PY[branch_idx : branch_idx + 2600]

    assert "source.save()" in block
    assert block.index("source.save()") < block.index("source_messages =")


def test_fork_from_message_snapshots_session_id_across_async_load():
    body = _function_body(COMMANDS_JS, "forkFromMessage")

    assert "const initialSid = S.session.session_id;" in body
    assert "S.session.session_id !== initialSid" in body
    assert "session_id:initialSid" in body
    assert "session_id:S.session.session_id" not in body


def test_fork_loads_full_fork_transcript_after_branch():
    body = _function_body(COMMANDS_JS, "forkFromMessage")

    load_idx = body.index("await loadSession(data.session_id)")
    after_load = body[load_idx:]
    assert "await _ensureAllMessagesLoaded()" in after_load
