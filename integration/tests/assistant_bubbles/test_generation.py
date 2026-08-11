import json
import time
from unittest.mock import patch

from integration.assistant_bubbles import collectors, generation, store


def test_first_fill_has_no_success_cooldown_between_categories(tmp_path):
    cache = None
    now = time.time()

    assert generation.should_generate("assistant_intro", "a", cache, now=now) is True
    assert generation.should_generate("memory", "b", cache, now=now) is True
    assert generation.should_generate("skill", "c", cache, now=now) is True
    assert generation.should_generate("emotion", "d", cache, now=now) is True


def test_changed_fingerprint_respects_success_cooldown():
    now = 1000.0
    cache = store.empty_store()
    cache["generation"]["skill"] = {
        "fingerprint": "old",
        "generated_at": now - 10,
        "last_attempt_at": now - 10,
        "retry_after": None,
    }

    assert generation.should_generate("skill", "new", cache, now=now) is False
    assert generation.should_generate("skill", "new", cache, now=now + 301) is True


def test_failure_retry_after_blocks_until_elapsed():
    now = 1000.0
    cache = store.empty_store()
    cache["generation"]["memory"] = {
        "fingerprint": "same",
        "generated_at": None,
        "last_attempt_at": now - 1,
        "retry_after": now + 30,
    }

    assert generation.should_generate("memory", "same", cache, now=now) is False
    assert generation.should_generate("memory", "same", cache, now=now + 31) is True


def test_emotion_refreshes_after_5_minutes():
    now = 100000.0
    cache = store.empty_store()
    cache["generation"]["emotion"] = {
        "fingerprint": "same",
        "generated_at": now - generation.EMOTION_REFRESH_SECONDS + 1,
        "last_attempt_at": now - 1000,
        "retry_after": None,
    }

    assert generation.should_generate("emotion", "same", cache, now=now) is False
    cache["generation"]["emotion"]["generated_at"] = now - generation.EMOTION_REFRESH_SECONDS - 1
    assert generation.should_generate("emotion", "same", cache, now=now) is True


def test_collect_skills_maps_local_all_display_metadata():
    raw = [
        {
            "name": "ideation",
            "display_name": "创意构思",
            "display_description": "生成创意点子",
            "description": "Generate ideas.",
        },
        {
            "name": "plain-english",
            "display_name": "",
            "description": "Generate concise English summaries.",
        },
    ]

    with patch(
        "integration.skills.listing.list_local_all_enabled_skills",
        return_value=raw,
    ):
        skills = collectors.collect_skills("default")

    assert skills == [
        {"name": "ideation", "label": "创意构思", "description": "生成创意点子"},
        {
            "name": "plain-english",
            "label": "plain-english",
            "description": "Generate concise English summaries.",
        },
    ]
    assert collectors.skills_block(skills) == (
        "- 创意构思：生成创意点子\n"
        "- plain-english：Generate concise English summaries."
    )


def test_collect_skills_uses_profile_name_for_local_all():
    calls: list[str] = []

    def fake_list(profile: str):
        calls.append(profile)
        return [{"name": "a", "display_name": "甲", "description": "desc"}]

    with patch(
        "integration.skills.listing.list_local_all_enabled_skills",
        side_effect=fake_list,
    ):
        skills = collectors.collect_skills("team-a")

    assert calls == ["team-a"]
    assert skills == [{"name": "a", "label": "甲", "description": "desc"}]


def test_skill_prompt_uses_full_skill_count_and_block():
    skills = [
        {"name": f"skill-{idx}", "label": f"技能{idx}", "description": f"描述{idx}"}
        for idx in range(260)
    ]
    context = {"skills_count": len(skills), "skills": skills}

    prompt = generation._load_user_prompt("skill", context)

    assert "- skills_count: 260" in prompt
    assert "- 技能0：描述0" in prompt
    assert "- 技能259：描述259" in prompt


def test_validate_emotion_requires_four_unique_short_texts():
    valid = '["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]'
    result, reason = generation.validate_model_output("emotion", valid)
    assert result == ["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]
    assert reason == "ok"
    duplicate = '["我在这里。😊", "我在这里。😊", "保持专注。✨", "随时叫我。😄"]'
    result, reason = generation.validate_model_output("emotion", duplicate)
    assert result is None
    assert reason == "emotion_duplicate"


def test_system_prompt_for_emotion_requires_json_array():
    emotion_prompt = generation._system_prompt_for("emotion")
    intro_prompt = generation._system_prompt_for("assistant_intro")
    assert emotion_prompt != intro_prompt
    assert "JSON 数组" in emotion_prompt
    assert "不得输出 JSON" in intro_prompt
    assert "句中必须使用逗号" in intro_prompt


def test_truncate_for_log_limits_user_prompt_display():
    text = "x" * 1200
    truncated = generation._truncate_for_log(text, generation.USER_PROMPT_LOG_MAX_CHARS)
    assert generation.USER_PROMPT_LOG_MAX_CHARS == 500
    assert truncated.startswith("x" * generation.USER_PROMPT_LOG_MAX_CHARS)
    assert truncated.endswith("(truncated, total 1200 chars)")
    assert generation._truncate_for_log("short", generation.USER_PROMPT_LOG_MAX_CHARS) == "short"


def test_assistant_intro_prompt_includes_punctuation_fewshot():
    prompt = generation._load_user_prompt(
        "assistant_intro",
        {"display_name": "小极助理", "description": "通用岗位智能协助", "soul": ""},
    )
    assert "句中必须使用逗号断句" in prompt
    assert "错误：哈喽我是小极助理擅长" in prompt
    assert "正确：哈喽，我是小极助理，擅长" in prompt


def test_validate_emotion_accepts_json_code_fence():
    content = '```json\n["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]\n```'
    result, reason = generation.validate_model_output("emotion", content)
    assert result == ["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]
    assert reason == "ok"


def test_validate_emotion_accepts_prefix_before_json_array():
    content = '好的：["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]'
    result, reason = generation.validate_model_output("emotion", content)
    assert result == ["我在这里。", "一起推进。💪", "保持专注。✨", "随时叫我。😄"]
    assert reason == "ok"


def test_validate_emotion_rejects_plain_text():
    content = "今日也要加油打工哦💪！"
    result, reason = generation.validate_model_output("emotion", content)
    assert result is None
    assert reason == "invalid_json"


def test_validate_model_output_allows_text_over_50_chars():
    content = "这是一段超过五十字但格式合法的气泡文案，用于确认本地代码不再强制校验长度，只依赖提示词约束模型尽量控制长度。"

    result, reason = generation.validate_model_output("assistant_intro", content)

    assert result == content
    assert reason == "ok"


def test_validate_skill_allows_real_chinese_skill_content():
    context = {
        "skills": [
            {"name": "ppt-deck-builder", "label": "PPT 生成", "description": "根据主题生成 PPT"},
            {"name": "pdf-toolkit", "label": "PDF 处理", "description": "处理 PDF 文件"},
        ]
    }

    result, reason = generation.validate_model_output(
        "skill",
        "我具备PPT 生成、PDF 处理等2项技能，擅长处理文档。",
        context,
    )

    assert result == "我具备PPT 生成、PDF 处理等2项技能，擅长处理文档。"
    assert reason == "ok"


def test_validate_skill_rejects_ascii_slugs_but_allows_chinese_summaries():
    context = {
        "skills": [
            {"name": "ideation", "label": "创意构思", "description": "生成创意点子"},
            {"name": "pixel-art", "label": "像素画", "description": "绘制像素风格图像"},
        ]
    }

    result, reason = generation.validate_model_output(
        "skill",
        "我具备ideation、pixel-art等2项专业工作，擅长创意设计。",
        context,
    )
    assert result is None
    assert reason == "skill_ascii_slug"

    result, reason = generation.validate_model_output(
        "skill",
        "我具备PPT、邮件、知识库等50项技能，擅长文档与通信。",
        context,
    )
    assert result == "我具备PPT、邮件、知识库等50项技能，擅长文档与通信。"
    assert reason == "ok"


def test_scheduled_task_stats_counts_completed_non_failed_jobs_as_pending(tmp_path):
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "enabled": False,
                        "state": "completed",
                        "last_status": "ok",
                        "next_run_at": None,
                    },
                    {
                        "enabled": False,
                        "state": "completed",
                        "last_status": "ok",
                        "next_run_at": None,
                    },
                    {
                        "enabled": True,
                        "state": "pending",
                        "next_run_at": "2026-07-20T20:00:00+08:00",
                    },
                    {"enabled": True, "state": "running"},
                    {"enabled": False, "state": "completed", "last_status": "failed", "last_error": "boom"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert collectors.scheduled_task_stats(tmp_path) == {
        "total": 5,
        "running": 1,
        "pending": 3,
        "failed": 1,
    }


def test_model_route_reads_profile_default_model(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "model:\n  provider: openai-compatible\n  default: local-model\nproviders:\n  openai-compatible:\n    base_url: http://127.0.0.1:1234/v1\n",
        encoding="utf-8",
    )

    assert collectors.model_route(profile) == {
        "provider": "openai-compatible",
        "model": "local-model",
        "base_url": "http://127.0.0.1:1234/v1",
    }


def test_model_route_normalizes_provider_model_picker(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  default: '@openrouter:anthropic/claude'\n", encoding="utf-8")

    assert collectors.model_route(profile)["provider"] == "openrouter"
    assert collectors.model_route(profile)["model"] == "anthropic/claude"


def test_run_task_logs_generation_success(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  provider: test\n  default: test-model\n", encoding="utf-8")
    context = collectors.collect_context("alice", profile, "assistant_intro")
    fingerprint = collectors.fingerprint_for("assistant_intro", context)
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint=fingerprint,
        provider="test",
        model="test-model",
    )

    with patch("integration.assistant_bubbles.generation._generate_with_model", return_value=("成功文案。", "ok")):
        generation._run_task(task)

    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][generation_succeeded]" in err
    assert "profile=alice" in err
    assert "category=assistant_intro" in err
    assert "reason=model_generated" in err
    assert "output_chars=5" in err
    assert "成功文案。" not in err


def test_generate_with_model_logs_call_success(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    context = collectors.collect_context("alice", profile, "assistant_intro")
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint="fp",
        provider="test",
        model="test-model",
    )
    response = type("Resp", (), {"choices": [type("Choice", (), {"message": type("Msg", (), {"content": "成功文案。"})()})()]})()

    with patch("integration.assistant_bubbles.generation.importlib.import_module") as import_module, patch(
        "api.profiles.profile_env_for_background_worker"
    ):
        import_module.return_value.call_llm.return_value = response
        result, reason = generation._generate_with_model(task, profile, context)

    assert result == "成功文案。"
    assert reason == "ok"
    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][model_prompt]" in err
    assert "----- system_prompt -----" in err
    assert "----- user_prompt -----" in err
    assert "----- end -----" in err
    assert generation.SYSTEM_PROMPT.splitlines()[0] in err
    assert "category=assistant_intro" in err
    assert "[webui][assistant_bubbles][model_response]" in err
    assert "----- model_response -----" in err
    assert "成功文案。" in err
    assert "[webui][assistant_bubbles][model_call_succeeded]" in err
    assert "profile=alice" in err
    assert "provider=test" in err
    assert "model=test-model" in err
    assert "elapsed_ms=" in err
    assert "output_chars=5" in err


def test_generate_with_model_logs_call_failure(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    context = collectors.collect_context("alice", profile, "assistant_intro")
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint="fp",
        provider="test",
        model="test-model",
    )

    with patch("integration.assistant_bubbles.generation.importlib.import_module") as import_module:
        import_module.return_value.call_llm.side_effect = RuntimeError("provider timeout")
        with patch("api.profiles.profile_env_for_background_worker"):
            result, reason = generation._generate_with_model(task, profile, context)

    assert result is None
    assert reason == "model_call_failed"
    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][model_prompt]" in err
    assert "----- system_prompt -----" in err
    assert "----- user_prompt -----" in err
    assert "[webui][assistant_bubbles][model_call_failed]" in err
    assert "profile=alice" in err
    assert "category=assistant_intro" in err
    assert "elapsed_ms=" in err
    assert "provider timeout" in err


def test_generate_with_model_logs_output_rejection_without_content(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    context = collectors.collect_context("alice", profile, "assistant_intro")
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint="fp",
        provider="test",
        model="test-model",
    )
    rejected_content = "SECRET_SENSITIVE_OUTPUT_12345\nsecond line"
    response = type("Resp", (), {"choices": [type("Choice", (), {"message": type("Msg", (), {"content": rejected_content})()})()]})()

    with patch("integration.assistant_bubbles.generation.importlib.import_module") as import_module:
        import_module.return_value.call_llm.return_value = response
        with patch("api.profiles.profile_env_for_background_worker"):
            result, reason = generation._generate_with_model(task, profile, context)

    assert result is None
    assert reason == "multiline"
    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][model_response]" in err
    assert "SECRET_SENSITIVE_OUTPUT_12345" in err
    assert "[webui][assistant_bubbles][model_output_rejected]" in err
    assert "reason=multiline" in err
    assert "elapsed_ms=" in err
    assert "output_chars=" in err


def test_run_task_skill_failure_writes_fallback_when_no_cached_text(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  provider: test\n  default: test-model\n", encoding="utf-8")
    local_skills = [
        {"name": "writer", "display_name": "写作", "description": "写作辅助"},
    ]
    with patch(
        "integration.skills.listing.list_local_all_enabled_skills",
        return_value=local_skills,
    ):
        context = collectors.collect_context("alice", profile, "skill")
        fingerprint = collectors.fingerprint_for("skill", context)
        task = generation.BubbleTask(
            profile="alice",
            profile_path=str(profile),
            category="skill",
            fingerprint=fingerprint,
            provider="test",
            model="test-model",
        )

        with patch(
            "integration.assistant_bubbles.generation._generate_with_model",
            return_value=(None, "skill_name_missing"),
        ), patch(
            "integration.skills.listing.list_local_all_enabled_skills",
            return_value=local_skills,
        ):
            generation._run_task(task)

    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][generation_failed]" in err
    assert "cache_action=fallback_written" in err
    cached = store.read_store(profile)
    assert cached is not None
    skill_item = next(item for item in cached["items"] if item["type"] == "skill")
    assert skill_item["text"]
    assert skill_item["text"] != "null"
    assert cached["generation"]["skill"]["generated_at"] is None
    assert cached["generation"]["skill"]["retry_after"] is not None


def test_run_task_skill_failure_preserves_existing_cached_text(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    data = store.empty_store()
    for item in data["items"]:
        if item["type"] == "emotion":
            item["text"] = "我在这里。"
        elif item["type"] == "skill":
            item["text"] = "旧技能文案。"
    data["generation"]["skill"] = {
        "fingerprint": "old",
        "generated_at": 1,
        "last_attempt_at": 1,
        "retry_after": None,
    }
    store.write_store(profile, data)

    generation._write_failure(
        profile,
        "skill",
        "new-fingerprint",
        reason="model_call_failed",
        task=generation.BubbleTask("alice", str(profile), "skill", "new-fingerprint", "test", "test-model"),
        context={"skills_count": 2},
    )

    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][generation_failed]" in err
    assert "cache_action=cached_text_preserved" in err
    cached = store.read_store(profile)
    skill_item = next(item for item in cached["items"] if item["type"] == "skill")
    assert skill_item["text"] == "旧技能文案。"
    assert cached["generation"]["skill"]["generated_at"] == 1
    assert cached["generation"]["skill"]["retry_after"] is not None


def test_run_task_logs_generation_failure(tmp_path, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model:\n  provider: test\n  default: test-model\n", encoding="utf-8")
    context = collectors.collect_context("alice", profile, "assistant_intro")
    fingerprint = collectors.fingerprint_for("assistant_intro", context)
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint=fingerprint,
        provider="test",
        model="test-model",
    )

    with patch("integration.assistant_bubbles.generation._generate_with_model", return_value=(None, "model_output_rejected")):
        generation._run_task(task)

    err = capsys.readouterr().err
    assert "[webui][assistant_bubbles][generation_failed]" in err
    assert "profile=alice" in err
    assert "category=assistant_intro" in err
    assert "reason=model_output_rejected" in err
    assert "cache_action=fallback_written" in err


def test_run_task_without_default_model_writes_fallback_without_call_llm(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "info.json").write_text('{"display_name":"小助理","description":"写文档"}', encoding="utf-8")
    context = collectors.collect_context("alice", profile, "assistant_intro")
    fingerprint = collectors.fingerprint_for("assistant_intro", context)
    task = generation.BubbleTask(
        profile="alice",
        profile_path=str(profile),
        category="assistant_intro",
        fingerprint=fingerprint,
        provider=None,
        model=None,
    )

    with patch("integration.assistant_bubbles.generation._generate_with_model") as call_model:
        generation._run_task(task)

    call_model.assert_not_called()
    cached = store.read_store(profile)
    assert cached is not None
    assert cached["items"][0]["type"] == "assistant_intro"
    assert cached["items"][0]["text"]
