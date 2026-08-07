import json
from pathlib import Path
from unittest.mock import patch

import pytest

from integration.common_tasks import collectors


# ---------- fingerprint_for_cluster ----------


def test_fingerprint_for_cluster_is_deterministic_for_same_questions():
    questions = [{"text": "写周报"}, {"text": "生成周报"}, {"text": "整理会议纪要"}]
    assert collectors.fingerprint_for_cluster(questions) == collectors.fingerprint_for_cluster(questions)


def test_fingerprint_for_cluster_ignores_order():
    qs_a = [{"text": "写周报"}, {"text": "生成周报"}]
    qs_b = [{"text": "生成周报"}, {"text": "写周报"}]
    assert collectors.fingerprint_for_cluster(qs_a) == collectors.fingerprint_for_cluster(qs_b)


def test_fingerprint_for_cluster_dedupes_same_text():
    qs = [{"text": "写周报"}, {"text": "写周报"}, {"text": "写周报"}]
    qs_unique = [{"text": "写周报"}]
    assert collectors.fingerprint_for_cluster(qs) == collectors.fingerprint_for_cluster(qs_unique)


def test_fingerprint_for_cluster_changes_when_questions_change():
    a = collectors.fingerprint_for_cluster([{"text": "写周报"}])
    b = collectors.fingerprint_for_cluster([{"text": "生成周报"}])
    assert a != b


def test_fingerprint_for_cluster_includes_prompt_version():
    payload = json.dumps(
        {"v": collectors.PROMPT_VERSION_CLUSTER, "qs": ["写周报"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert collectors.fingerprint_for_cluster([{"text": "写周报"}]) == expected


def test_fingerprint_for_cluster_handles_empty_list():
    assert collectors.fingerprint_for_cluster([]) != ""
    assert collectors.fingerprint_for_cluster([]) == collectors.fingerprint_for_cluster([])


# ---------- collect_recent_user_questions ----------


@pytest.fixture
def sessions_dir(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr("api.config.SESSION_DIR", sessions)
    return sessions


def _write_session(sessions_dir: Path, sid: str, profile: str, messages: list, *, session_id: str | None = None):
    payload = {
        "session_id": session_id or sid,
        "profile": profile,
        "messages": messages,
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_collect_recent_user_questions_filters_by_profile(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [{"role": "user", "content": "你好", "timestamp": 1.0}],
    )
    _write_session(
        sessions_dir,
        "s2",
        "bob",
        [{"role": "user", "content": "你好", "timestamp": 2.0}],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1
    assert questions[0]["text"] == "你好"
    assert questions[0]["session_id"] == "s1"


def test_collect_recent_user_questions_skips_slash_commands(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [
            {"role": "user", "content": "/help", "timestamp": 1.0},
            {"role": "user", "content": "正常问题", "timestamp": 2.0},
        ],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1
    assert questions[0]["text"] == "正常问题"


def test_collect_recent_user_questions_skips_empty_text(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [
            {"role": "user", "content": "   ", "timestamp": 1.0},
            {"role": "user", "content": "", "timestamp": 2.0},
            {"role": "user", "content": "有效", "timestamp": 3.0},
        ],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1
    assert questions[0]["text"] == "有效"


def test_collect_recent_user_questions_skips_non_user_messages(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [
            {"role": "assistant", "content": "你好", "timestamp": 1.0},
            {"role": "system", "content": "system", "timestamp": 2.0},
            {"role": "user", "content": "用户提问", "timestamp": 3.0},
        ],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1
    assert questions[0]["text"] == "用户提问"


def test_collect_recent_user_questions_sorts_by_timestamp_desc(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [
            {"role": "user", "content": "旧", "timestamp": 1.0},
            {"role": "user", "content": "新", "timestamp": 100.0},
            {"role": "user", "content": "中", "timestamp": 50.0},
        ],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert [q["text"] for q in questions] == ["新", "中", "旧"]


def test_collect_recent_user_questions_respects_limit(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [{"role": "user", "content": f"q{i}", "timestamp": float(i)} for i in range(20)],
    )

    questions = collectors.collect_recent_user_questions("alice", limit=5)
    assert len(questions) == 5
    assert questions[0]["text"] == "q19"


def test_collect_recent_user_questions_handles_multimodal_content(sessions_dir):
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
                "timestamp": 1.0,
            }
        ],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1
    assert questions[0]["text"] == "看这张图"


def test_collect_recent_user_questions_skips_files_starting_with_underscore(sessions_dir):
    (sessions_dir / "_index.json").write_text(
        json.dumps({"profile": "alice", "messages": [{"role": "user", "content": "不应被读取", "timestamp": 1.0}]}),
        encoding="utf-8",
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert questions == []


def test_collect_recent_user_questions_skips_invalid_json(sessions_dir):
    (sessions_dir / "broken.json").write_text("not json", encoding="utf-8")
    _write_session(
        sessions_dir,
        "s1",
        "alice",
        [{"role": "user", "content": "有效", "timestamp": 1.0}],
    )

    questions = collectors.collect_recent_user_questions("alice")
    assert len(questions) == 1


def test_collect_recent_user_questions_defaults_profile_to_default_when_missing(sessions_dir):
    (sessions_dir / "s1.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": "默认提问", "timestamp": 1.0}]}),
        encoding="utf-8",
    )

    questions = collectors.collect_recent_user_questions("default")
    assert len(questions) == 1
    assert questions[0]["text"] == "默认提问"


def test_collect_recent_user_questions_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    missing = tmp_path / "no-such-dir"
    monkeypatch.setattr("api.config.SESSION_DIR", missing)
    assert collectors.collect_recent_user_questions("alice") == []


def test_collect_recent_user_questions_skips_session_without_messages_list(sessions_dir):
    (sessions_dir / "s1.json").write_text(
        json.dumps({"profile": "alice", "messages": "not-a-list"}),
        encoding="utf-8",
    )

    assert collectors.collect_recent_user_questions("alice") == []


# ---------- render_cluster_user_prompt ----------


def test_render_cluster_user_prompt_numbers_and_dedupes():
    questions = [
        {"text": "写周报"},
        {"text": "写周报"},
        {"text": "生成周报"},
        {"text": "整理会议纪要"},
    ]

    prompt = collectors.render_cluster_user_prompt(questions)
    numbered_section = prompt.split("要求:")[0]

    assert "1. 写周报" in numbered_section
    assert "2. 生成周报" in numbered_section
    assert "3. 整理会议纪要" in numbered_section
    assert numbered_section.count("1. 写周报") == 1
    assert "4. " not in numbered_section
    assert "提出的 3 条问题" in prompt


def test_render_cluster_user_prompt_handles_empty():
    prompt = collectors.render_cluster_user_prompt([])
    assert "(无)" in prompt


def test_render_cluster_user_prompt_skips_empty_text():
    questions = [{"text": ""}, {"text": "  "}, {"text": "有效"}]

    prompt = collectors.render_cluster_user_prompt(questions)

    assert "1. 有效" in prompt
    assert "2. " not in prompt


# ---------- render_seed_user_prompt ----------


def test_render_seed_user_prompt_fills_template():
    context = {
        "display_name": "小极助理",
        "description": "通用岗位智能协助",
        "soul": "我是小极助理",
        "skills": [{"name": "writer", "label": "写作", "description": "写作辅助"}],
    }

    prompt = collectors.render_seed_user_prompt(context)

    assert "小极助理" in prompt
    assert "通用岗位智能协助" in prompt
    assert "我是小极助理" in prompt
    assert "- 写作：写作辅助" in prompt


def test_render_seed_user_prompt_handles_empty_skills():
    context = {
        "display_name": "小助",
        "description": "",
        "soul": "",
        "skills": [],
    }

    prompt = collectors.render_seed_user_prompt(context)
    assert "小助" in prompt


# ---------- collect_seed_context ----------


def test_collect_seed_context_includes_persona_and_skills(tmp_path):
    (tmp_path / "info.json").write_text(
        json.dumps({"display_name": "小助", "description": "通用助手"}),
        encoding="utf-8",
    )
    (tmp_path / "SOUL.md").write_text("我是小助", encoding="utf-8")
    local_skills = [{"name": "writer", "display_name": "写作", "description": "写作辅助"}]

    with patch(
        "integration.skills.listing.list_local_all_enabled_skills",
        return_value=local_skills,
    ):
        context = collectors.collect_seed_context("alice", tmp_path)

    assert context["profile"] == "alice"
    assert context["display_name"] == "小助"
    assert context["description"] == "通用助手"
    assert context["soul"] == "我是小助"
    assert context["skills_count"] == 1
    assert context["skills"][0]["label"] == "写作"


def test_collect_seed_context_defaults_display_name_to_profile(tmp_path):
    context = collectors.collect_seed_context("alice", tmp_path)
    assert context["display_name"] == "alice"


def test_collect_seed_context_defaults_display_name_to_hermes_when_no_profile(tmp_path):
    context = collectors.collect_seed_context("", tmp_path)
    assert context["display_name"] == "Hermes"