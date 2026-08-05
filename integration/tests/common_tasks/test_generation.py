import json
import time
from unittest.mock import patch

from integration.common_tasks import collectors, generation, store


def _make_job(profile_path, *, kind="seed", fingerprint="", provider="test", model="test-model"):
    return generation.CommonTaskJob(
        profile="alice",
        profile_path=str(profile_path),
        kind=kind,
        fingerprint=fingerprint,
        provider=provider,
        model=model,
    )


def _llm_response(content: str):
    return type(
        "Resp",
        (),
        {"choices": [type("Choice", (), {"message": type("Msg", (), {"content": content})()})()]},
    )()


# ---------- validate_seed_response ----------


def test_validate_seed_response_valid_array_of_three():
    payload = json.dumps(
        [
            {"title": "整理会议纪要", "description": "提取决议与待办", "trigger_language": "帮我整理会议纪要"},
            {"title": "起草周报", "description": "汇总本周工作", "trigger_language": "帮我写本周周报"},
            {"title": "起草邮件", "description": "生成商务邮件", "trigger_language": "帮我写一封邮件"},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert len(tasks) == 3
    assert tasks[0] == {
        "title": "整理会议纪要",
        "description": "提取决议与待办",
        "trigger_language": "帮我整理会议纪要",
        "query_count": 0,
        "source": "seed",
        "members_json": "[]",
    }


def test_validate_seed_response_accepts_code_fence():
    payload = '```json\n' + json.dumps(
        [
            {"title": "甲", "trigger_language": "t1"},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    ) + '\n```'

    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert len(tasks) == 3


def test_validate_seed_response_accepts_prefix_before_array():
    payload = '好的：' + json.dumps(
        [
            {"title": "甲", "trigger_language": "t1"},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert len(tasks) == 3


def test_validate_seed_response_rejects_non_string():
    tasks, reason = generation.validate_seed_response(None)
    assert tasks is None
    assert reason == "not_string"


def test_validate_seed_response_rejects_invalid_json():
    tasks, reason = generation.validate_seed_response("not json")
    assert tasks is None
    assert reason == "invalid_json"


def test_validate_seed_response_rejects_non_array():
    tasks, reason = generation.validate_seed_response('{"a": 1}')
    assert tasks is None
    assert reason == "not_json_array"


def test_validate_seed_response_rejects_count_not_three():
    payload = json.dumps(
        [
            {"title": "甲", "trigger_language": "t1"},
            {"title": "乙", "trigger_language": "t2"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert tasks is None
    assert reason == "seed_count_not_3"


def test_validate_seed_response_rejects_item_not_dict():
    payload = json.dumps(["甲", "乙", "丙"])
    tasks, reason = generation.validate_seed_response(payload)
    assert tasks is None
    assert reason == "seed_item_not_dict"


def test_validate_seed_response_rejects_missing_field():
    payload = json.dumps(
        [
            {"title": "甲"},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert tasks is None
    assert reason == "seed_missing_field"


def test_validate_seed_response_rejects_duplicate_title():
    payload = json.dumps(
        [
            {"title": "重复", "trigger_language": "t1"},
            {"title": "重复", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert tasks is None
    assert reason == "seed_duplicate_title"


def test_validate_seed_response_truncates_overlong_fields():
    payload = json.dumps(
        [
            {"title": "标题" * 20, "description": "描述" * 30, "trigger_language": "触发" * 20},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert len(tasks[0]["title"]) <= 15
    assert len(tasks[0]["description"]) <= 50
    assert len(tasks[0]["trigger_language"]) <= 30


def test_validate_seed_response_strips_wrapping_quotes():
    payload = json.dumps(
        [
            {"title": '"甲"', "trigger_language": "'t1'"},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert tasks[0]["title"] == "甲"
    assert tasks[0]["trigger_language"] == "t1"


def test_validate_seed_response_keeps_mismatched_curly_quotes():
    payload = json.dumps(
        [
            {"title": "“甲”", "trigger_language": "“t1”"},
            {"title": "乙", "trigger_language": "t2"},
            {"title": "丙", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )
    tasks, reason = generation.validate_seed_response(payload)
    assert reason == "ok"
    assert tasks[0]["title"] == "“甲”"


# ---------- validate_cluster_response ----------


def test_validate_cluster_response_returns_top_n_sorted_by_count():
    questions = {"q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10"}
    payload = json.dumps(
        [
            {
                "title": "簇A",
                "description": "A描述",
                "trigger_language": "tA",
                "members": ["q1", "q2", "q3", "q4"],
                "count": 4,
            },
            {
                "title": "簇B",
                "description": "B描述",
                "trigger_language": "tB",
                "members": ["q5", "q6", "q7"],
                "count": 3,
            },
            {
                "title": "簇C",
                "description": "C描述",
                "trigger_language": "tC",
                "members": ["q8", "q9", "q10"],
                "count": 3,
            },
            {
                "title": "簇D",
                "description": "D描述",
                "trigger_language": "tD",
                "members": ["q1", "q5"],
                "count": 2,
            },
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert len(tasks) == generation.TOP_N
    assert tasks[0]["title"] == "簇A"
    assert tasks[0]["query_count"] == 4
    assert tasks[0]["source"] == "mined"
    assert tasks[0]["members_json"] == json.dumps(["q1", "q2", "q3", "q4"], ensure_ascii=False)
    # 簇D was filtered out (count < 3) so only 3 clusters remain, exactly TOP_N.
    assert {t["title"] for t in tasks} == {"簇A", "簇B", "簇C"}


def test_validate_cluster_response_filters_members_not_in_original():
    questions = {"real1", "real2", "real3"}
    payload = json.dumps(
        [
            {
                "title": "簇A",
                "trigger_language": "tA",
                "members": ["real1", "real2", "real3", "fabricated"],
                "count": 4,
            },
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert tasks[0]["members_json"] == json.dumps(["real1", "real2", "real3"], ensure_ascii=False)
    assert tasks[0]["query_count"] == 3


def test_validate_cluster_response_skips_clusters_with_fewer_than_three_members():
    questions = {"q1", "q2", "q3", "q4", "q5"}
    payload = json.dumps(
        [
            {"title": "簇A", "trigger_language": "tA", "members": ["q1", "q2"], "count": 2},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert tasks is None
    assert reason == "no_valid_cluster"


def test_validate_cluster_response_skips_non_string_members():
    questions = {"q1", "q2", "q3"}
    payload = json.dumps(
        [
            {"title": "簇A", "trigger_language": "tA", "members": ["q1", "q2", "q3"], "count": 3},
            {"title": "簇B", "trigger_language": "tB", "members": ["q1", "q2", 3], "count": 3},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert len(tasks) == 1
    assert tasks[0]["title"] == "簇A"


def test_validate_cluster_response_skips_missing_required_fields():
    questions = {"q1", "q2", "q3"}
    payload = json.dumps(
        [
            {"title": "", "trigger_language": "tA", "members": ["q1", "q2", "q3"]},
            {"title": "簇B", "trigger_language": "", "members": ["q1", "q2", "q3"]},
            {"title": "簇C", "trigger_language": "tC"},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert tasks is None
    assert reason == "no_valid_cluster"


def test_validate_cluster_response_dedupes_by_title():
    questions = {"q1", "q2", "q3", "q4", "q5", "q6"}
    payload = json.dumps(
        [
            {"title": "重复", "trigger_language": "tA", "members": ["q1", "q2", "q3"], "count": 3},
            {"title": "重复", "trigger_language": "tB", "members": ["q4", "q5", "q6"], "count": 3},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert len(tasks) == 1


def test_validate_cluster_response_rejects_non_string():
    tasks, reason = generation.validate_cluster_response(None, set())
    assert tasks is None
    assert reason == "not_string"


def test_validate_cluster_response_rejects_invalid_json():
    tasks, reason = generation.validate_cluster_response("not json", set())
    assert tasks is None
    assert reason == "invalid_json"


# ---------- _should_mine ----------


def test_should_mine_blocks_when_retry_after_in_future():
    now = time.time()
    state = {"retry_after": str(now + 30)}
    assert generation._should_mine("fp", state) is False


def test_should_mine_blocks_same_fingerprint_within_success_cooldown():
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_success_at": str(now - 10),
        "retry_after": "",
    }
    assert generation._should_mine("fp", state) is False

    state["last_success_at"] = str(now - generation.SUCCESS_COOLDOWN_SECONDS - 1)
    assert generation._should_mine("fp", state) is True


def test_should_mine_blocks_different_fingerprint_within_failure_retry():
    now = time.time()
    state = {
        "last_fingerprint": "old",
        "last_attempt_at": str(now - 1),
        "retry_after": "",
        "last_success_at": "",
    }
    assert generation._should_mine("new", state) is False

    state["last_attempt_at"] = str(now - generation.FAILURE_RETRY_SECONDS - 1)
    assert generation._should_mine("new", state) is True


def test_should_mine_allows_when_state_empty():
    assert generation._should_mine("fp", {}) is True


def test_should_mine_allows_same_fingerprint_after_cooldown_with_no_success():
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - 100),
        "last_success_at": "",
        "retry_after": "",
    }
    assert generation._should_mine("fp", state) is True


# ---------- _fallback_seed_tasks ----------


def test_fallback_seed_tasks_uses_first_short_cjk_skill_label():
    context = {
        "display_name": "小助",
        "description": "通用助手",
        "skills": [
            {"name": "english-only", "label": "english-only"},  # ASCII slug skipped
            {"name": "too-long-skill", "label": "这是一个超过十字以上的技能标签"},  # too long
            {"name": "writing", "label": "写作"},  # OK
            {"name": "ignored", "label": "邮件"},  # not first
        ],
    }

    tasks = generation._fallback_seed_tasks(context)
    assert len(tasks) == 3
    assert tasks[0]["title"] == "用写作"
    assert "写作" in tasks[0]["description"]
    assert tasks[0]["trigger_language"] == "用写作帮我处理一下"
    assert all(t["source"] == "seed" and t["members_json"] == "[]" for t in tasks)


def test_fallback_seed_tasks_uses_description_when_no_cjk_skill():
    context = {
        "display_name": "文书助手",
        "description": "整理会议纪要与周报",
        "skills": [{"name": "english-only", "label": "english-only"}],
    }

    tasks = generation._fallback_seed_tasks(context)
    assert len(tasks) == 3
    assert tasks[0]["title"] == "整理会议纪要与周报"[:15]
    assert "文书助手" in tasks[0]["description"]


def test_fallback_seed_tasks_uses_default_when_no_skill_no_description():
    context = {"display_name": "小助", "description": "", "skills": []}

    tasks = generation._fallback_seed_tasks(context)
    assert len(tasks) == 3
    assert tasks[0]["title"] == "概述当前工作"
    assert "小助" in tasks[0]["description"]
    assert {t["title"] for t in tasks} == {"概述当前工作", "整理待办", "总结对话"}


def test_fallback_seed_tasks_defaults_display_name_to_hermes():
    tasks = generation._fallback_seed_tasks({"display_name": "", "description": "", "skills": []})
    assert len(tasks) == 3
    assert "Hermes" in tasks[0]["description"]


# ---------- enqueue_missing_or_stale ----------


def test_enqueue_missing_or_stale_blocks_when_retry_after_in_future(tmp_path):
    store.write_seed_placeholder(
        tmp_path,
        [{"title": "占位", "trigger_language": "t"}],
        retry_after=time.time() + 60,
        last_error="boom",
    )
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert enqueued == []


def test_enqueue_missing_or_stale_enqueues_seed_when_no_seed_generated_at(tmp_path):
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][0] == "alice"
    assert enqueued[0][2] == "seed"


def test_enqueue_missing_or_stale_skips_when_too_few_unique_questions(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "种子", "trigger_language": "t"}])
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=[{"text": "q1"}],
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert enqueued == []


def test_enqueue_missing_or_stale_enqueues_mine_when_questions_sufficient_and_stale(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "种子", "trigger_language": "t"}])
    # write_seed_tasks stamps last_attempt_at = now; back-date it past FAILURE_RETRY_SECONDS
    # so _should_mine does not treat this as a recent failed attempt on a new fingerprint.
    store.update_state(tmp_path, "last_attempt_at", str(time.time() - 60))
    questions = [{"text": f"q{i}"} for i in range(10)]
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "mine"
    assert enqueued[0][3] == collectors.fingerprint_for_cluster(questions)


def test_enqueue_missing_or_stale_skips_mine_when_within_success_cooldown(tmp_path):
    fp = collectors.fingerprint_for_cluster([{"text": f"q{i}"} for i in range(10)])
    store.write_seed_tasks(tmp_path, [{"title": "种子", "trigger_language": "t"}])
    store.replace_mined_tasks(
        tmp_path,
        [{"title": "挖掘", "trigger_language": "t", "members_json": "[]"}],
        fp,
        success=True,
    )
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=[{"text": f"q{i}"} for i in range(10)],
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert enqueued == []


# ---------- _run_seed ----------


def test_run_seed_writes_fallback_when_no_model(tmp_path, capsys):
    job = _make_job(tmp_path, kind="seed", provider=None, model=None)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ), patch("integration.common_tasks.generation._call_llm") as call_llm:
        generation._run_seed(job, tmp_path)

    call_llm.assert_not_called()
    rows, state = store.read_all(tmp_path)
    assert len(rows) == 3
    assert state.get("seed_generated_at")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_succeeded]" in err
    assert "reason=missing_profile_default_model_fallback" in err


def test_run_seed_success_writes_seed_tasks(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: test\n  default: test-model\n",
        encoding="utf-8",
    )
    job = _make_job(tmp_path, kind="seed")
    context = {"display_name": "小助", "description": "通用助手", "skills": []}
    payload = json.dumps(
        [
            {"title": "甲", "description": "d1", "trigger_language": "t1"},
            {"title": "乙", "description": "d2", "trigger_language": "t2"},
            {"title": "丙", "description": "d3", "trigger_language": "t3"},
        ],
        ensure_ascii=False,
    )

    with patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ), patch(
        "integration.common_tasks.generation._call_llm", return_value=(payload, "ok")
    ) as call_llm:
        generation._run_seed(job, tmp_path)

    assert call_llm.call_args.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    rows, state = store.read_all(tmp_path)
    assert len(rows) == 3
    assert {r["title"] for r in rows} == {"甲", "乙", "丙"}
    assert state.get("seed_generated_at")
    assert state.get("retry_after", "") == ""
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_succeeded]" in err
    assert "reason=model_generated" in err
    assert "count=3" in err


def test_run_seed_failure_writes_placeholder_with_retry_after(tmp_path, capsys):
    job = _make_job(tmp_path, kind="seed")
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ), patch(
        "integration.common_tasks.generation._call_llm", return_value=(None, "model_call_failed")
    ):
        generation._run_seed(job, tmp_path)

    rows, state = store.read_all(tmp_path)
    assert len(rows) == 3  # fallback placeholder rows
    assert state.get("seed_generated_at", "") == ""
    assert float(state["retry_after"]) >= time.time()
    assert state["last_error"] == "model_call_failed"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_failed]" in err
    assert "reason=model_call_failed" in err
    assert "cache_action=fallback_written" in err


def test_run_seed_rejected_response_writes_placeholder(tmp_path, capsys):
    job = _make_job(tmp_path, kind="seed")
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=("not json", "ok"),
    ):
        generation._run_seed(job, tmp_path)

    _, state = store.read_all(tmp_path)
    assert state.get("seed_generated_at", "") == ""
    assert state["last_error"].startswith("seed_")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_failed]" in err


# ---------- _run_mine ----------


def test_run_mine_fingerprint_mismatch_re_arms_with_fresh_fingerprint(tmp_path):
    job = _make_job(tmp_path, kind="mine", fingerprint="stale-fp")
    questions = [{"text": f"q{i}"} for i in range(10)]

    enqueued = []
    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_mine(job, tmp_path)

    fresh_fp = collectors.fingerprint_for_cluster(questions)
    assert len(enqueued) == 1
    assert enqueued[0][2] == "mine"
    assert enqueued[0][3] == fresh_fp


def test_run_mine_missing_model_records_failure(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp, provider=None, model=None)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all(tmp_path)
    assert rows == []
    assert state.get("last_attempt_at")
    assert float(state["retry_after"]) >= time.time()
    assert state["last_error"] == "missing_model"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_skipped]" in err
    assert "reason=missing_profile_default_model" in err


def test_run_mine_too_few_unique_questions_skips_without_writing(tmp_path):
    questions = [{"text": "q1"}, {"text": "q1"}, {"text": "q1"}, {"text": "q2"}]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation._call_llm") as call_llm:
        generation._run_mine(job, tmp_path)

    call_llm.assert_not_called()
    rows, state = store.read_all(tmp_path)
    assert rows == []
    assert state == {}


def test_run_mine_truncates_long_questions_before_prompting(tmp_path):
    long_text = "x" * 200
    questions = [{"text": long_text}] + [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=(None, "model_call_failed"),
    ) as call_llm:
        generation._run_mine(job, tmp_path)

    user_prompt = call_llm.call_args.kwargs["user_prompt"]
    assert long_text not in user_prompt
    assert ("x" * generation.MAX_QUESTION_CHARS + "…") in user_prompt


def test_run_mine_failure_records_error(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=(None, "model_call_failed"),
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all(tmp_path)
    assert rows == []
    assert state["last_error"] == "model_call_failed"
    assert float(state["retry_after"]) >= time.time()
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_failed]" in err


def test_run_mine_rejected_response_records_cluster_reason(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=("not json", "ok"),
    ):
        generation._run_mine(job, tmp_path)

    _, state = store.read_all(tmp_path)
    assert state["last_error"].startswith("cluster_")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_rejected]" in err


def test_run_mine_success_replaces_mined_tasks(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    payload = json.dumps(
        [
            {
                "title": "簇A",
                "description": "A描述",
                "trigger_language": "tA",
                "members": [f"q{i}" for i in range(4)],
                "count": 4,
            },
            {
                "title": "簇B",
                "description": "B描述",
                "trigger_language": "tB",
                "members": [f"q{i}" for i in range(4, 8)],
                "count": 4,
            },
            {
                "title": "簇C",
                "description": "C描述",
                "trigger_language": "tC",
                "members": ["q8", "q9", "q0"],
                "count": 3,
            },
        ],
        ensure_ascii=False,
    )

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=(payload, "ok"),
    ) as call_llm:
        generation._run_mine(job, tmp_path)

    assert call_llm.call_args.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call_llm.call_args.kwargs["max_tokens"] == 4000
    rows, state = store.read_all(tmp_path)
    mined = [r for r in rows if r["source"] == "mined"]
    assert len(mined) == 3
    assert state["last_fingerprint"] == fp
    assert state.get("last_success_at")
    assert state.get("retry_after", "") == ""
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_succeeded]" in err
    assert "count=3" in err


# ---------- _call_llm ----------


def test_call_llm_success_returns_content(tmp_path, capsys):
    job = _make_job(tmp_path)

    with patch("integration.common_tasks.generation.importlib.import_module") as import_module, patch(
        "api.profiles.profile_env_for_background_worker"
    ):
        import_module.return_value.call_llm.return_value = _llm_response("回复内容")
        content, reason = generation._call_llm(
            job,
            "sys",
            "user",
            max_tokens=100,
            temperature=0.5,
            extra_body={"thinking": {"type": "disabled"}},
        )

    assert content == "回复内容"
    assert reason == "ok"
    call_kwargs = import_module.return_value.call_llm.call_args.kwargs
    assert call_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call_kwargs["max_tokens"] == 100
    assert call_kwargs["temperature"] == 0.5
    assert call_kwargs["timeout"] == 60
    assert call_kwargs["task"] == "common_tasks"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][model_call_succeeded]" in err
    assert "output_chars=4" in err


def test_call_llm_failure_returns_none(tmp_path, capsys):
    job = _make_job(tmp_path)

    with patch("integration.common_tasks.generation.importlib.import_module") as import_module:
        import_module.return_value.call_llm.side_effect = RuntimeError("provider timeout")
        with patch("api.profiles.profile_env_for_background_worker"):
            content, reason = generation._call_llm(
                job,
                "sys",
                "user",
                max_tokens=100,
                temperature=0.5,
            )

    assert content is None
    assert reason == "model_call_failed"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][model_call_failed]" in err
    assert "provider timeout" in err


def test_call_llm_passes_extra_body_none_by_default(tmp_path):
    job = _make_job(tmp_path)

    with patch("integration.common_tasks.generation.importlib.import_module") as import_module, patch(
        "api.profiles.profile_env_for_background_worker"
    ):
        import_module.return_value.call_llm.return_value = _llm_response("ok")
        generation._call_llm(job, "sys", "user", max_tokens=10, temperature=0.1)

    assert import_module.return_value.call_llm.call_args.kwargs["extra_body"] is None