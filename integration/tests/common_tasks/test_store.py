import time

from integration.common_tasks import store


def test_db_path_under_profile_home(tmp_path):
    assert store.db_path(tmp_path) == tmp_path / "common_tasks.db"


def test_read_all_returns_empty_when_db_missing(tmp_path):
    assert store.read_all(tmp_path) == ([], {})


def test_write_seed_tasks_round_trip_stamps_seed_generated_at(tmp_path):
    tasks = [
        {"title": "整理会议纪要", "description": "提取决议与待办", "trigger_language": "帮我整理会议纪要"},
        {"title": "起草周报", "description": "汇总本周工作", "trigger_language": "帮我写本周周报"},
        {"title": "起草邮件", "description": "生成商务邮件", "trigger_language": "帮我写一封邮件"},
    ]

    store.write_seed_tasks(tmp_path, tasks)
    rows, state = store.read_all(tmp_path)

    assert len(rows) == 3
    assert all(row["source"] == "seed" for row in rows)
    assert {row["title"] for row in rows} == {"整理会议纪要", "起草周报", "起草邮件"}
    assert state.get("seed_generated_at")
    assert state.get("last_attempt_at")
    assert state.get("retry_after", "") == ""
    assert state.get("last_error", "") == ""


def test_write_seed_tasks_truncates_overlong_fields(tmp_path):
    tasks = [
        {
            "title": "标题" * 20,
            "description": "描述" * 30,
            "trigger_language": "触发" * 20,
        },
    ]

    store.write_seed_tasks(tmp_path, tasks)
    rows, _ = store.read_all(tmp_path)

    assert len(rows) == 1
    assert len(rows[0]["title"]) <= 15
    assert len(rows[0]["description"]) <= 50
    assert len(rows[0]["trigger_language"]) <= 30


def test_write_seed_tasks_replaces_previous_seed_rows(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "旧任务", "trigger_language": "旧触发"}])
    store.write_seed_tasks(
        tmp_path,
        [
            {"title": "新任务A", "trigger_language": "触发A"},
            {"title": "新任务B", "trigger_language": "触发B"},
        ],
    )

    rows, _ = store.read_all(tmp_path)
    assert {row["title"] for row in rows} == {"新任务A", "新任务B"}


def test_write_seed_placeholder_does_not_stamp_seed_generated_at(tmp_path):
    fallback = [{"title": "占位任务", "trigger_language": "占位触发"}]
    retry_after = time.time() + 60

    store.write_seed_placeholder(tmp_path, fallback, retry_after=retry_after, last_error="boom")
    rows, state = store.read_all(tmp_path)

    assert len(rows) == 1
    assert rows[0]["title"] == "占位任务"
    assert state.get("seed_generated_at", "") == ""
    assert float(state["retry_after"]) == retry_after
    assert state["last_error"] == "boom"
    assert state.get("last_attempt_at")


def test_write_seed_placeholder_clears_existing_seed_generated_at(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "真种子", "trigger_language": "种子触发"}])
    _, state_before = store.read_all(tmp_path)
    assert state_before.get("seed_generated_at")

    store.write_seed_placeholder(
        tmp_path,
        [{"title": "占位", "trigger_language": "占位触发"}],
        retry_after=time.time() + 30,
        last_error="model_call_failed",
    )
    _, state_after = store.read_all(tmp_path)
    assert state_after.get("seed_generated_at", "") == ""
    assert state_after["last_error"] == "model_call_failed"


def test_replace_mined_tasks_success_clears_retry_after_and_stamps_fingerprint(tmp_path):
    tasks = [
        {
            "title": "生成周报",
            "description": "汇总本周工作",
            "trigger_language": "帮我写周报",
            "query_count": 4,
            "members_json": '["帮我写周报","生成周报"]',
        },
    ]

    store.replace_mined_tasks(tmp_path, tasks, "fp-123", success=True)
    rows, state = store.read_all(tmp_path)

    assert len(rows) == 1
    assert rows[0]["source"] == "mined"
    assert rows[0]["fingerprint"] == "fp-123"
    assert rows[0]["query_count"] == 4
    assert rows[0]["members_json"] == '["帮我写周报","生成周报"]'
    assert state["last_fingerprint"] == "fp-123"
    assert state.get("last_success_at")
    assert state.get("last_attempt_at")
    assert state.get("retry_after", "") == ""
    assert state.get("last_error", "") == ""


def test_replace_mined_tasks_failure_keeps_old_mined_and_sets_retry_after(tmp_path):
    old = [
        {
            "title": "旧任务",
            "description": "旧描述",
            "trigger_language": "旧触发",
            "query_count": 5,
            "members_json": "[]",
        }
    ]
    store.replace_mined_tasks(tmp_path, old, "fp-old", success=True)

    store.replace_mined_tasks(
        tmp_path,
        [],
        "fp-new",
        success=False,
        last_error="cluster_no_valid_cluster",
    )
    rows, state = store.read_all(tmp_path)

    assert len(rows) == 1
    assert rows[0]["title"] == "旧任务"
    assert rows[0]["fingerprint"] == "fp-old"
    assert state["last_fingerprint"] == "fp-old"
    assert float(state["retry_after"]) >= time.time()
    assert state["last_error"] == "cluster_no_valid_cluster"
    assert state.get("last_attempt_at")


def test_replace_mined_tasks_replaces_previous_mined_rows_on_success(tmp_path):
    store.replace_mined_tasks(
        tmp_path,
        [{"title": "旧挖掘", "trigger_language": "旧触发", "members_json": "[]"}],
        "fp-old",
        success=True,
    )
    store.replace_mined_tasks(
        tmp_path,
        [{"title": "新挖掘", "trigger_language": "新触发", "members_json": "[]"}],
        "fp-new",
        success=True,
    )

    rows, _ = store.read_all(tmp_path)
    mined = [r for r in rows if r["source"] == "mined"]
    assert len(mined) == 1
    assert mined[0]["title"] == "新挖掘"
    assert mined[0]["fingerprint"] == "fp-new"


def test_pick_top3_mined_first_then_seed_up_to_3(tmp_path):
    tasks = [
        {"title": "种子1", "description": "d1", "trigger_language": "t1", "query_count": 0, "source": "seed"},
        {"title": "种子2", "description": "d2", "trigger_language": "t2", "query_count": 0, "source": "seed"},
        {"title": "挖掘A", "description": "dA", "trigger_language": "tA", "query_count": 5, "source": "mined"},
        {"title": "挖掘B", "description": "dB", "trigger_language": "tB", "query_count": 3, "source": "mined"},
        {"title": "挖掘C", "description": "dC", "trigger_language": "tC", "query_count": 1, "source": "mined"},
    ]

    top3 = store.pick_top3(tasks)
    assert [item["title"] for item in top3] == ["挖掘A", "挖掘B", "挖掘C"]
    assert all("id" not in item and "fingerprint" not in item for item in top3)


def test_pick_top3_fills_remaining_with_seed_when_mined_has_fewer_than_3(tmp_path):
    tasks = [
        {"title": "种子1", "description": "d1", "trigger_language": "t1", "query_count": 0, "source": "seed"},
        {"title": "种子2", "description": "d2", "trigger_language": "t2", "query_count": 0, "source": "seed"},
        {"title": "挖掘A", "description": "dA", "trigger_language": "tA", "query_count": 5, "source": "mined"},
    ]

    top3 = store.pick_top3(tasks)
    assert [item["title"] for item in top3] == ["挖掘A", "种子1", "种子2"]


def test_pick_top3_dedup_by_title(tmp_path):
    tasks = [
        {"title": "重复", "description": "d1", "trigger_language": "t1", "query_count": 0, "source": "seed"},
        {"title": "重复", "description": "d2", "trigger_language": "t2", "query_count": 5, "source": "mined"},
        {"title": "其他", "description": "d3", "trigger_language": "t3", "query_count": 0, "source": "seed"},
    ]

    top3 = store.pick_top3(tasks)
    assert [item["title"] for item in top3] == ["重复", "其他"]


def test_pick_top3_skips_empty_title(tmp_path):
    tasks = [
        {"title": "", "description": "d1", "trigger_language": "t1", "query_count": 0, "source": "seed"},
        {"title": "  ", "description": "d2", "trigger_language": "t2", "query_count": 5, "source": "mined"},
        {"title": "有效", "description": "d3", "trigger_language": "t3", "query_count": 0, "source": "seed"},
    ]

    top3 = store.pick_top3(tasks)
    assert [item["title"] for item in top3] == ["有效"]


def test_pick_top3_returns_empty_when_input_empty():
    assert store.pick_top3([]) == []


def test_has_mined_and_has_seed():
    tasks = [
        {"source": "seed"},
        {"source": "mined"},
    ]
    assert store.has_mined(tasks) is True
    assert store.has_seed(tasks) is True

    seed_only = [{"source": "seed"}]
    assert store.has_mined(seed_only) is False
    assert store.has_seed(seed_only) is True

    mined_only = [{"source": "mined"}]
    assert store.has_mined(mined_only) is True
    assert store.has_seed(mined_only) is False

    assert store.has_mined([]) is False
    assert store.has_seed([]) is False


def test_update_state_upserts_key(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "x", "trigger_language": "y"}])

    store.update_state(tmp_path, "custom_key", "v1")
    _, state = store.read_all(tmp_path)
    assert state["custom_key"] == "v1"

    store.update_state(tmp_path, "custom_key", "v2")
    _, state = store.read_all(tmp_path)
    assert state["custom_key"] == "v2"


def test_update_state_clears_key_when_value_is_none_or_empty(tmp_path):
    store.write_seed_tasks(tmp_path, [{"title": "x", "trigger_language": "y"}])
    store.update_state(tmp_path, "custom_key", "v1")
    store.update_state(tmp_path, "custom_key", None)

    _, state = store.read_all(tmp_path)
    assert state.get("custom_key", "") == ""


def test_update_state_no_op_when_db_missing(tmp_path):
    store.update_state(tmp_path, "custom_key", "v1")
    assert not (tmp_path / "common_tasks.db").exists()