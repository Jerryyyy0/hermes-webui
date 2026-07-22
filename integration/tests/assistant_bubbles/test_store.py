import json

from integration.assistant_bubbles import store


def test_store_round_trip_independent_file(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "info.json").write_text(json.dumps({"display_name": "Demo"}), encoding="utf-8")
    data = store.empty_store()
    data["items"][0]["text"] = "你好，我是 Demo。"
    data["items"][1]["text"] = "我在这里。"
    data["items"][3]["text"] = "一起推进。"
    data["items"][4]["text"] = "我记住了新事项。"
    data["items"][5]["text"] = "保持专注。"
    data["items"][6]["text"] = "我有写作技能。"
    data["items"][7]["text"] = "随时叫我。"
    data["generation"]["assistant_intro"] = {
        "fingerprint": "abc",
        "generated_at": 1,
        "last_attempt_at": 1,
        "retry_after": None,
    }

    store.write_store(profile, data)

    assert (profile / "assistant_bubbles.json").is_file()
    assert json.loads((profile / "info.json").read_text(encoding="utf-8")) == {"display_name": "Demo"}
    assert store.read_store(profile)["items"][2] == {"type": "scheduled_task", "text": None, "dynamic": True}


def test_invalid_schema_is_cache_miss(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "assistant_bubbles.json").write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    assert store.read_store(profile) is None


def test_accepts_text_over_50_chars(tmp_path):
    data = store.empty_store()
    data["items"][0]["text"] = "x" * 80

    store.write_store(tmp_path, data)

    assert store.read_store(tmp_path)["items"][0]["text"] == "x" * 80
