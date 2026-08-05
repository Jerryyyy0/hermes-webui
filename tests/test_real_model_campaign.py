from scripts.real_model_campaign import SCENARIOS, build_prompt, cancellation_plan, evaluate_alignment, seed_scenario


def test_default_cancel_plan_covers_all_paths():
    assert cancellation_plan(15) == {3: "immediate_after_start", 6: "after_first_tool", 8: "after_manifest_delta", 12: "after_first_artifact"}


def test_prompt_reuses_previous_artifact_and_has_turn_path():
    prompt, path = build_prompt(SCENARIOS[0], 3, "nonce-3", "deliverables/turn-02/delivery.md")
    assert "turn-02" in prompt and "NONCE=nonce-3" in prompt and path.endswith("turn-03/delivery.md")


def test_seed_scenario_creates_cross_format_inputs(tmp_path):
    seed_scenario(tmp_path, SCENARIOS[0])
    assert (tmp_path / "input/orders.csv").exists() and (tmp_path / "input/events.json").exists()


def test_missing_artifact_is_not_an_alignment_failure(tmp_path):
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {"turns": [{"turn_key": "turn:1", "artifacts": []}], "diagnostics": {"orphan_turn_keys": []}}
    failures, observations, _ = evaluate_alignment(session, manifest, {"nonce": "nonce", "turn_key": "turn:1"}, tmp_path, set())
    assert not failures and observations == [{"code": "MODEL_NO_ARTIFACT"}]


def test_artifact_with_two_turn_owners_fails_alignment(tmp_path):
    file = tmp_path / "deliverables/turn-01/delivery.md"; file.parent.mkdir(parents=True); file.write_text("nonce", encoding="utf-8")
    session = {"messages": [{"role": "user", "content": "nonce", "_turn_key": "turn:1"}]}
    manifest = {"turns": [{"turn_key": "turn:1", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}]}, {"turn_key": "turn:2", "artifacts": [{"path": "deliverables/turn-01/delivery.md"}]}], "diagnostics": {"orphan_turn_keys": []}}
    failures, _, _ = evaluate_alignment(session, manifest, {"nonce": "nonce", "turn_key": "turn:1"}, tmp_path, {"deliverables/turn-01/delivery.md"})
    assert any(issue["code"] == "ARTIFACT_MULTI_TURN_OWNER" for issue in failures)
