import json
import time
from unittest.mock import patch

import pytest

from integration.common_tasks import collectors, generation, store


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch, tmp_path):
    """Route common_tasks store reads/writes at tmp_path/session_manifest.db.

    generation._run_seed/_run_mine call store.write_* with task.profile and no
    db_path override, so they resolve to STATE_DIR/session_manifest.db. Pin
    that to the test's tmp_path to keep tests hermetic.
    """
    monkeypatch.setattr("integration.common_tasks.store.STATE_DIR", tmp_path)


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
    # 簇D count=2 通过校验但排第 4,被 TOP_N 截断
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


def test_validate_cluster_response_skips_clusters_with_fewer_than_two_members():
    questions = {"q1", "q2", "q3", "q4", "q5"}
    payload = json.dumps(
        [
            {"title": "簇A", "trigger_language": "tA", "members": ["q1"], "count": 1},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert tasks is None
    assert reason == "no_valid_cluster"


def test_validate_cluster_response_accepts_cluster_with_two_members():
    questions = {"q1", "q2"}
    payload = json.dumps(
        [
            {"title": "簇A", "trigger_language": "tA", "members": ["q1", "q2"], "count": 2},
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert len(tasks) == 1
    assert tasks[0]["query_count"] == 2


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
        "last_attempt_at": str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1),
        "last_success_at": "",
        "retry_after": "",
    }
    assert generation._should_mine("fp", state) is True


def test_should_mine_blocks_same_fingerprint_within_failure_cooldown_no_success():
    """fp 不变且从未成功:last_attempt 在 MINE_FAILURE_COOLDOWN_SECONDS 内返回 False,
    避免 mine 持续失败时 hammer LLM,期间由 enqueue_missing_or_stale 入队 seed 兜底."""
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - 10),
        "last_success_at": "",
        "retry_after": "",
        "last_error": "cluster_no_valid_cluster",
    }
    assert generation._should_mine("fp", state) is False

    state["last_attempt_at"] = str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1)
    assert generation._should_mine("fp", state) is True


def test_should_mine_blocks_longer_when_last_error_is_model_call_failed():
    """LLM 调用失败(model_call_failed)冷却 MINE_LLM_FAILURE_COOLDOWN_SECONDS(600s),
    期间不再重试 mine,由 seed 兜底;超过后才允许重试."""
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1),
        "last_success_at": "",
        "retry_after": "",
        "last_error": "model_call_failed",
    }
    # 30 秒已过但 600 秒未到:LLM 失败冷却仍然阻断
    assert generation._should_mine("fp", state) is False

    state["last_attempt_at"] = str(now - generation.MINE_LLM_FAILURE_COOLDOWN_SECONDS - 1)
    assert generation._should_mine("fp", state) is True


def test_should_mine_blocks_longer_when_last_error_is_seed_model_call_failed():
    """seed 失败会覆盖 mine 的 last_error(变成 seed_model_call_failed),
    也要走 600s 冷却,避免 mine 被 seed 失败污染后频繁 hammer LLM."""
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1),
        "last_success_at": "",
        "retry_after": "",
        "last_error": "seed_model_call_failed",
    }
    assert generation._should_mine("fp", state) is False

    state["last_attempt_at"] = str(now - generation.MINE_LLM_FAILURE_COOLDOWN_SECONDS - 1)
    assert generation._should_mine("fp", state) is True


def test_should_mine_blocks_longer_when_last_error_is_missing_model():
    """missing_model 是配置问题,短时间不会自愈,走 600s 冷却."""
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1),
        "last_success_at": "",
        "retry_after": "",
        "last_error": "missing_model",
    }
    assert generation._should_mine("fp", state) is False

    state["last_attempt_at"] = str(now - generation.MINE_LLM_FAILURE_COOLDOWN_SECONDS - 1)
    assert generation._should_mine("fp", state) is True


def test_should_mine_uses_short_cooldown_for_cluster_validation_error():
    """cluster 校验失败(cluster_no_valid_cluster)走 30s 冷却,可能是 prompt 偶发问题."""
    now = time.time()
    state = {
        "last_fingerprint": "fp",
        "last_attempt_at": str(now - 10),
        "last_success_at": "",
        "retry_after": "",
        "last_error": "cluster_no_valid_cluster",
    }
    assert generation._should_mine("fp", state) is False

    state["last_attempt_at"] = str(now - generation.MINE_FAILURE_COOLDOWN_SECONDS - 1)
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


# ---------- enqueue_missing_or_stale (fast path, non-blocking) ----------


def test_enqueue_missing_or_stale_blocks_when_retry_after_in_future(tmp_path):
    store.write_seed_placeholder(
        "alice",
        [{"title": "占位", "trigger_language": "t"}],
        retry_after=time.time() + 60,
        last_error="boom",
    )
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert enqueued == []


def test_enqueue_missing_or_stale_skips_when_mined_and_seed_fresh(tmp_path):
    """mined + seed 都有且在成功冷却期内 -> 直接跳过,不入队任何任务."""
    fp = collectors.fingerprint_for_cluster([{"text": f"q{i}"} for i in range(10)])
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    store.replace_mined_tasks(
        "alice",
        [{"title": "挖掘", "trigger_language": "t", "members_json": "[]"}],
        fp,
        success=True,
    )
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert enqueued == []


def test_enqueue_missing_or_stale_enqueues_check_when_no_seed(tmp_path):
    """没有 seed 数据 -> 入队 check 让后台决定."""
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][0] == "alice"
    assert enqueued[0][2] == "check"


def test_enqueue_missing_or_stale_enqueues_check_when_stale_mined(tmp_path):
    """有 mined 数据但已过冷却期 -> 入队 check 让后台重扫."""
    fp = collectors.fingerprint_for_cluster([{"text": f"q{i}"} for i in range(10)])
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    store.replace_mined_tasks(
        "alice",
        [{"title": "挖掘", "trigger_language": "t", "members_json": "[]"}],
        fp,
        success=True,
    )
    store.update_state("alice", "last_success_at", str(time.time() - 3600))
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "check"


def test_enqueue_missing_or_stale_enqueues_check_when_only_seed(tmp_path):
    """只有 seed 没有 mined -> 入队 check,后台会扫 session 看是否能 mine."""
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation.enqueue_missing_or_stale("alice", tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "check"


# ---------- _run_check (worker-side decision + session scan) ----------


def _make_check_job(tmp_path, *, profile="alice"):
    return generation.CommonTaskJob(
        profile=profile,
        profile_path=str(tmp_path),
        kind="check",
        fingerprint="",
    )


def test_run_check_enqueues_seed_when_no_seed_generated_at(tmp_path):
    job = _make_check_job(tmp_path)
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(job, tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][0] == "alice"
    assert enqueued[0][2] == "seed"


def test_run_check_enqueues_mine_when_questions_sufficient(tmp_path):
    """session 问题足够多且没有 seed -> 直接 mine."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][0] == "alice"
    assert enqueued[0][2] == "mine"
    assert enqueued[0][3] == collectors.fingerprint_for_cluster(questions)


def test_run_check_skips_when_retry_after_in_future(tmp_path):
    store.write_seed_placeholder(
        "alice",
        [{"title": "占位", "trigger_language": "t"}],
        retry_after=time.time() + 60,
        last_error="boom",
    )
    enqueued = []

    with patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert enqueued == []


def test_run_check_seed_when_too_few_questions_and_no_seed(tmp_path):
    """问题不够多且没有 seed -> 入队 seed 兜底."""
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=[{"text": "q1"}],
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "seed"


def test_run_check_noop_when_too_few_questions_but_seed_exists(tmp_path):
    """问题不够多但已有 seed -> 什么都不做."""
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=[{"text": "q1"}],
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert enqueued == []


def test_run_check_enqueues_mine_when_stale_fingerprint(tmp_path):
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    store.update_state("alice", "last_attempt_at", str(time.time() - 60))
    questions = [{"text": f"q{i}"} for i in range(10)]
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "mine"
    assert enqueued[0][3] == collectors.fingerprint_for_cluster(questions)


def test_run_check_skips_mine_when_within_success_cooldown(tmp_path):
    fp = collectors.fingerprint_for_cluster([{"text": f"q{i}"} for i in range(10)])
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    store.replace_mined_tasks(
        "alice",
        [{"title": "挖掘", "trigger_language": "t", "members_json": "[]"}],
        fp,
        success=True,
    )
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=[{"text": f"q{i}"} for i in range(10)],
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert enqueued == []


def test_run_check_enqueues_seed_when_mine_cooldown_and_no_cache(tmp_path):
    """mine 冷却中且无 seed -> 入队 seed 兜底."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    store.replace_mined_tasks(
        "alice",
        [],
        fp,
        success=False,
        last_error="cluster_no_valid_cluster",
    )
    store.update_state("alice", "retry_after", str(time.time() - 1))
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert len(enqueued) == 1
    assert enqueued[0][2] == "seed"


def test_run_check_skips_seed_when_mine_cooldown_but_seed_present(tmp_path):
    """mine 冷却中但已有 seed -> 不重复入队."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    store.write_seed_tasks("alice", [{"title": "种子", "trigger_language": "t"}])
    store.replace_mined_tasks(
        "alice",
        [],
        fp,
        success=False,
        last_error="cluster_no_valid_cluster",
    )
    store.update_state("alice", "retry_after", str(time.time() - 1))
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    assert enqueued == []


def test_run_check_repairs_stale_seed_stamp(tmp_path, capsys):
    """check 时发现 seed_generated_at 已设但无 seed 行 -> 清空 stamp 并入队 seed."""
    store.write_seed_tasks("alice", [{"title": "真种子", "trigger_language": "t"}])
    store.delete_seed_rows("alice")
    questions = [{"text": "q1"}, {"text": "q2"}]
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_check(_make_check_job(tmp_path), tmp_path)

    _, state = store.read_all("alice")
    assert state.get("seed_generated_at", "") == ""
    assert len(enqueued) == 1
    assert enqueued[0][2] == "seed"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_integrity_repaired]" in err


# ---------- _repair_stale_seed_stamp ----------


def test_repair_stale_seed_stamp_clears_stamp_when_no_seed_rows(tmp_path, capsys):
    """seed_generated_at 已设置但表里无 seed 行 -> 清空 stamp 并 emit warning."""
    store.write_seed_tasks("alice", [{"title": "真种子", "trigger_language": "t"}])
    # Simulate seed rows being lost (manual delete / DB issue)
    store.delete_seed_rows("alice")
    _, state_before = store.read_all("alice")
    assert state_before.get("seed_generated_at")

    generation._repair_stale_seed_stamp("alice")

    _, state_after = store.read_all("alice")
    assert state_after.get("seed_generated_at", "") == ""
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_integrity_repaired]" in err
    assert "seed_generated_at_without_seed_rows" in err


def test_repair_stale_seed_stamp_noop_when_seed_rows_present(tmp_path, capsys):
    """seed_generated_at 已设置且表里有 seed 行 -> 不动."""
    store.write_seed_tasks("alice", [{"title": "真种子", "trigger_language": "t"}])
    stamp_before = store.read_all("alice")[1].get("seed_generated_at")

    generation._repair_stale_seed_stamp("alice")

    _, state_after = store.read_all("alice")
    assert state_after.get("seed_generated_at") == stamp_before
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_integrity_repaired]" not in err


def test_repair_stale_seed_stamp_noop_when_stamp_empty(tmp_path, capsys):
    """seed_generated_at 为空(未生成或 fallback 占位) -> 不动."""
    # Fallback seed rows: write_seed_rows_only does NOT stamp seed_generated_at
    store.write_seed_rows_only("alice", [{"title": "占位", "trigger_language": "t"}])

    generation._repair_stale_seed_stamp("alice")

    tasks, state = store.read_all("alice")
    assert state.get("seed_generated_at", "") == ""
    assert len(tasks) == 1  # fallback rows preserved
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_integrity_repaired]" not in err


def test_repair_stale_seed_stamp_noop_when_db_missing(tmp_path, capsys):
    """DB 不存在 -> 不创建 DB,不报错."""
    generation._repair_stale_seed_stamp("alice")
    assert not (tmp_path / "session_manifest.db").exists()
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_integrity_repaired]" not in err


def test_start_pregeneration_calls_repair_then_enqueue_for_each_profile(tmp_path, capsys):
    """start_pregeneration 对每个 profile 先 _repair_stale_seed_stamp 再 enqueue_missing_or_stale,
    且 repair 实际清掉 stale stamp."""
    store.write_seed_tasks("alice", [{"title": "真种子", "trigger_language": "t"}])
    store.delete_seed_rows("alice")  # create stale stamp
    profile_rows = [{"name": "alice", "path": str(tmp_path)}]

    call_order = []
    real_repair = generation._repair_stale_seed_stamp

    def _spy_repair(profile):
        call_order.append(("repair", profile))
        real_repair(profile)

    def _spy_enqueue(profile, path):
        call_order.append(("enqueue", profile, str(path)))

    import threading

    with patch("integration.common_tasks.generation._repair_stale_seed_stamp", side_effect=_spy_repair), patch(
        "integration.common_tasks.generation.enqueue_missing_or_stale", side_effect=_spy_enqueue
    ), patch("api.profiles.list_profiles_api", return_value=profile_rows):
        generation.start_pregeneration()
        # Join the daemon thread INSIDE the patch context so spies are still active
        for t in threading.enumerate():
            if t.name == "common-tasks-pregeneration":
                t.join(timeout=2.0)
                break

    assert call_order == [("repair", "alice"), ("enqueue", "alice", str(tmp_path))]
    _, state = store.read_all("alice")
    assert state.get("seed_generated_at", "") == ""


# ---------- _run_seed ----------


def test_run_seed_writes_fallback_when_no_model(tmp_path, capsys):
    job = _make_job(tmp_path, kind="seed", provider=None, model=None)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ), patch("integration.common_tasks.generation._call_llm") as call_llm:
        generation._run_seed(job, tmp_path)

    call_llm.assert_not_called()
    rows, state = store.read_all("alice")
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

    assert "extra_body" not in call_llm.call_args.kwargs
    rows, state = store.read_all("alice")
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

    rows, state = store.read_all("alice")
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

    _, state = store.read_all("alice")
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


def test_run_mine_fingerprint_mismatch_skips_rearm_when_retry_after_in_future(tmp_path, capsys):
    """retry_after 在未来时,fingerprint 不匹配也不 re-arm,避免 hammer 宕机的 LLM."""
    job = _make_job(tmp_path, kind="mine", fingerprint="stale-fp")
    questions = [{"text": f"q{i}"} for i in range(10)]
    # Create DB then set retry_after 200s into the future (simulating recent LLM failure)
    store.write_seed_tasks("alice", [{"title": "x", "trigger_language": "y"}])
    store.update_state("alice", "retry_after", str(time.time() + 200))

    enqueued = []
    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation.enqueue", lambda *args: enqueued.append(args)):
        generation._run_mine(job, tmp_path)

    assert enqueued == []
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_rearm_skipped]" in err
    assert "reason=retry_after_in_future" in err


def test_run_mine_fingerprint_mismatch_rearms_when_retry_after_expired(tmp_path, capsys):
    """retry_after 已过期时,fingerprint 不匹配仍正常 re-arm."""
    job = _make_job(tmp_path, kind="mine", fingerprint="stale-fp")
    questions = [{"text": f"q{i}"} for i in range(10)]
    # Create DB then set retry_after in the past
    store.write_seed_tasks("alice", [{"title": "x", "trigger_language": "y"}])
    store.update_state("alice", "retry_after", str(time.time() - 10))

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
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_rearm_skipped]" not in err


def test_run_mine_missing_model_records_failure(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp, provider=None, model=None)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    mined_rows = [r for r in rows if r["source"] == "mined"]
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert mined_rows == []
    assert len(seed_rows) > 0
    assert state.get("last_attempt_at")
    assert float(state["retry_after"]) >= time.time()
    assert state["last_error"] == "missing_model"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_skipped]" in err
    assert "reason=missing_profile_default_model" in err
    assert "[webui][common_tasks][seed_fallback_written]" in err


def test_run_mine_missing_model_skips_seed_fallback_when_seed_present(tmp_path, capsys):
    """mine_skipped 时若已有 seed 数据,不再重写 fallback 占位."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp, provider=None, model=None)
    store.write_seed_tasks("alice", [{"title": "已有种子", "trigger_language": "t"}])

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert len(seed_rows) == 1
    assert seed_rows[0]["title"] == "已有种子"
    assert state["last_error"] == "missing_model"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_fallback_written]" not in err


def test_run_mine_too_few_unique_questions_skips_without_writing(tmp_path, capsys):
    questions = [{"text": "q1"}, {"text": "q1"}, {"text": "q1"}, {"text": "q2"}]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch("integration.common_tasks.generation._call_llm") as call_llm:
        generation._run_mine(job, tmp_path)

    call_llm.assert_not_called()
    rows, state = store.read_all("alice")
    assert rows == []
    assert state == {}
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_skipped]" in err
    assert "reason=too_few_unique_questions" in err
    assert "dedup_count=2" in err


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


def test_validate_cluster_response_truncates_members_to_max():
    questions = {f"q{i}" for i in range(8)}
    payload = json.dumps(
        [
            {
                "title": "簇A",
                "description": "d",
                "trigger_language": "tA",
                "members": [f"q{i}" for i in range(8)],
                "count": 8,
            },
        ],
        ensure_ascii=False,
    )

    tasks, reason = generation.validate_cluster_response(payload, questions)
    assert reason == "ok"
    assert len(tasks) == 1
    members = json.loads(tasks[0]["members_json"])
    assert len(members) == generation.MAX_MEMBERS_PER_CLUSTER
    assert tasks[0]["query_count"] == generation.MAX_MEMBERS_PER_CLUSTER


def test_run_mine_failure_records_error(tmp_path, capsys):
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=(None, "model_call_failed"),
    ), patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    mined_rows = [r for r in rows if r["source"] == "mined"]
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert mined_rows == []
    assert len(seed_rows) > 0  # mine 失败后写入 fallback seed 占位
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

    _, state = store.read_all("alice")
    assert state["last_error"].startswith("cluster_")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_rejected]" in err


def test_run_mine_rejected_writes_seed_fallback_when_no_seed(tmp_path, capsys):
    """mine_rejected 后立即写 fallback seed 占位,避免接口返回 empty."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=("not json", "ok"),
    ), patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert len(seed_rows) > 0
    assert state["last_error"].startswith("cluster_")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_fallback_written]" in err


def test_run_mine_failed_writes_seed_fallback_when_no_seed(tmp_path, capsys):
    """mine_failed (LLM 调用失败) 后立即写 fallback seed 占位."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=(None, "model_call_failed"),
    ), patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert len(seed_rows) > 0
    assert state["last_error"] == "model_call_failed"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_failed]" in err
    assert "[webui][common_tasks][seed_fallback_written]" in err


def test_run_mine_skips_seed_fallback_when_seed_already_present(tmp_path, capsys):
    """已有 seed 数据时,mine 失败不重写 seed 行."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    store.write_seed_tasks("alice", [{"title": "已有种子", "trigger_language": "t"}])
    context = {"display_name": "小助", "description": "通用助手", "skills": []}

    with patch(
        "integration.common_tasks.collectors.collect_recent_user_questions",
        return_value=questions,
    ), patch(
        "integration.common_tasks.generation._call_llm",
        return_value=("not json", "ok"),
    ), patch(
        "integration.common_tasks.collectors.collect_seed_context", return_value=context
    ):
        generation._run_mine(job, tmp_path)

    rows, _ = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    assert len(seed_rows) == 1
    assert seed_rows[0]["title"] == "已有种子"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_fallback_written]" not in err


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

    assert "extra_body" not in call_llm.call_args.kwargs
    assert call_llm.call_args.kwargs["max_tokens"] == 4000
    rows, state = store.read_all("alice")
    mined = [r for r in rows if r["source"] == "mined"]
    assert len(mined) == 3
    assert state["last_fingerprint"] == fp
    assert state.get("last_success_at")
    assert state.get("retry_after", "") == ""
    err = capsys.readouterr().err
    assert "[webui][common_tasks][mine_succeeded]" in err
    assert "count=3" in err


def test_run_mine_success_clears_fallback_seed_rows(tmp_path, capsys):
    """mine 成功后清理 _ensure_seed_fallback 写入的占位 seed 行,避免污染 pick_top3."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    # Pre-write fallback seed rows (no seed_generated_at) like _ensure_seed_fallback does
    store.write_seed_rows_only("alice", [{"title": "占位seed", "trigger_language": "t"}])
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
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    mined_rows = [r for r in rows if r["source"] == "mined"]
    assert seed_rows == []
    assert len(mined_rows) == 3
    assert state.get("seed_generated_at", "") == ""
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_fallback_cleared]" in err


def test_run_mine_success_preserves_llm_generated_seed_rows(tmp_path, capsys):
    """mine 成功后保留 LLM 生成的 seed 行(seed_generated_at 已设置),用于补足 top3."""
    questions = [{"text": f"q{i}"} for i in range(10)]
    fp = collectors.fingerprint_for_cluster(questions)
    job = _make_job(tmp_path, kind="mine", fingerprint=fp)
    # Pre-write LLM-generated seed rows (stamps seed_generated_at)
    store.write_seed_tasks("alice", [{"title": "真种子", "trigger_language": "t"}])
    payload = json.dumps(
        [
            {
                "title": "簇A",
                "description": "A描述",
                "trigger_language": "tA",
                "members": [f"q{i}" for i in range(4)],
                "count": 4,
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
    ):
        generation._run_mine(job, tmp_path)

    rows, state = store.read_all("alice")
    seed_rows = [r for r in rows if r["source"] == "seed"]
    mined_rows = [r for r in rows if r["source"] == "mined"]
    assert len(seed_rows) == 1
    assert seed_rows[0]["title"] == "真种子"
    assert len(mined_rows) == 1
    assert state.get("seed_generated_at")
    err = capsys.readouterr().err
    assert "[webui][common_tasks][seed_fallback_cleared]" not in err


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
        )

    assert content == "回复内容"
    assert reason == "ok"
    call_kwargs = import_module.return_value.call_llm.call_args.kwargs
    assert call_kwargs["extra_body"]["thinking"] is False
    assert call_kwargs["extra_body"]["reasoning_effort"] == "off"
    assert call_kwargs["max_tokens"] == 100
    assert call_kwargs["temperature"] == 0.5
    assert call_kwargs["timeout"] == 120
    assert call_kwargs["task"] == "common_tasks"
    err = capsys.readouterr().err
    assert "[webui][common_tasks][model_call_succeeded]" in err
    assert "output_chars=4" in err


def test_disable_thinking_extra_body_qwen_uses_boolean():
    body = generation._disable_thinking_extra_body("qwen", "qwen3.8-32b-instruct")
    assert body == {"enable_thinking": False, "thinking": False}


def test_disable_thinking_extra_body_kimi_uses_object():
    body = generation._disable_thinking_extra_body("kimi", "kimi-k2.5")
    assert body == {"thinking": {"type": "disabled"}}


def test_disable_thinking_extra_body_deepseek_uses_thinking_disabled():
    body = generation._disable_thinking_extra_body("deepseek", "deepseek-r1")
    assert body == {"thinking": {"type": "disabled"}}


def test_disable_thinking_extra_body_default_sends_both():
    body = generation._disable_thinking_extra_body("test", "test-model")
    assert body["thinking"] is False
    assert body["reasoning_effort"] == "off"


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


def test_call_llm_injects_disable_thinking_by_default(tmp_path):
    job = _make_job(tmp_path)

    with patch("integration.common_tasks.generation.importlib.import_module") as import_module, patch(
        "api.profiles.profile_env_for_background_worker"
    ):
        import_module.return_value.call_llm.return_value = _llm_response("ok")
        generation._call_llm(job, "sys", "user", max_tokens=10, temperature=0.1)

    body = import_module.return_value.call_llm.call_args.kwargs["extra_body"]
    assert isinstance(body, dict)
    assert body["thinking"] is False
    assert body["reasoning_effort"] == "off"