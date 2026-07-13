"""Tests for Cron Hub execution-only inference fallback."""

from unittest.mock import patch

from integration.crons.execution_model import (
    _first_catalog_inference,
    prepare_cron_hub_execution_job,
)


def test_first_catalog_inference_preserves_order_and_provider_prefix():
    catalog = {
        "groups": [
            {
                "provider_id": "custom:packy",
                "models": [{"id": "@custom:packy:first"}, {"id": "second"}],
            },
            {"provider_id": "other", "models": [{"id": "third"}]},
        ]
    }
    assert _first_catalog_inference(catalog) == ("@custom:packy:first", "custom:packy")


def test_first_catalog_inference_uses_extra_models():
    catalog = {
        "groups": [
            {"provider": "openrouter", "models": [], "extra_models": [{"id": "overflow"}]}
        ]
    }
    assert _first_catalog_inference(catalog) == ("overflow", "openrouter")


def test_unpinned_job_uses_current_profile_inference_and_clears_execution_snapshots(tmp_path):
    original = {
        "id": "job1",
        "model": None,
        "provider": None,
        "model_snapshot": "glm-5.2",
        "provider_snapshot": "openrouter",
    }
    with patch(
        "integration.crons.execution_model._configured_inference",
        return_value=("deepseek-v4-flash", "custom"),
    ):
        execution = prepare_cron_hub_execution_job(original, "default", tmp_path)

    assert execution["model"] == "deepseek-v4-flash"
    assert execution["provider"] == "custom"
    assert execution["model_snapshot"] is None
    assert execution["provider_snapshot"] is None
    assert original["model"] is None
    assert original["provider"] is None
    assert original["model_snapshot"] == "glm-5.2"
    assert original["provider_snapshot"] == "openrouter"


def test_unpinned_job_uses_catalog_model_and_provider(tmp_path):
    original = {"id": "job1", "model": None, "provider": None}
    with patch("integration.crons.execution_model._configured_inference", return_value=("", "")):
        with patch("integration.crons.execution_model._environment_model", return_value=""):
            with patch(
                "integration.crons.execution_model._discover_first_inference",
                return_value=("catalog-model", "catalog-provider"),
            ):
                execution = prepare_cron_hub_execution_job(original, "ops", tmp_path)
    assert execution["model"] == "catalog-model"
    assert execution["provider"] == "catalog-provider"


def test_named_profile_falls_back_to_default_profile_inference(tmp_path):
    original = {"id": "job1", "model": None, "provider": None}
    default_home = tmp_path / "default"
    with patch(
        "integration.crons.execution_model._configured_inference",
        side_effect=[("", ""), ("default-model", "default-provider")],
    ):
        with patch("integration.crons.execution_model._environment_model", return_value=""):
            with patch(
                "integration.crons.execution_model._discover_first_inference",
                return_value=("", ""),
            ):
                with patch(
                    "api.profiles.get_hermes_home_for_profile",
                    return_value=default_home,
                ) as get_home:
                    execution = prepare_cron_hub_execution_job(original, "coder", tmp_path)

    get_home.assert_called_once_with("default")
    assert execution["model"] == "default-model"
    assert execution["provider"] == "default-provider"
    assert execution["model_snapshot"] is None
    assert execution["provider_snapshot"] is None


def test_discovery_runs_in_requested_profile(tmp_path):
    original = {"id": "job1", "model": None, "provider": None}
    catalog = {
        "groups": [{"provider_id": "ops-provider", "models": [{"id": "ops-model"}]}]
    }
    with patch("integration.crons.execution_model._configured_inference", return_value=("", "")):
        with patch("integration.crons.execution_model._environment_model", return_value=""):
            with patch("integration.crons.execution_model.get_active_profile_name", return_value="default"):
                with patch("integration.crons.execution_model.set_request_profile") as set_profile:
                    with patch(
                        "integration.crons.execution_model.get_available_models",
                        return_value=catalog,
                    ):
                        execution = prepare_cron_hub_execution_job(original, "ops", tmp_path)
    assert execution["model"] == "ops-model"
    assert set_profile.call_args_list[0].args == ("ops",)
    assert set_profile.call_args_list[-1].args == ("default",)


def test_explicit_inference_is_never_overridden(tmp_path):
    original = {
        "id": "job1",
        "model": "glm-5.2",
        "provider": "openrouter",
        "model_snapshot": "old",
    }
    with patch("integration.crons.execution_model._configured_inference") as configured:
        execution = prepare_cron_hub_execution_job(original, "ops", tmp_path)
    assert execution == original
    assert execution is not original
    configured.assert_not_called()


def test_no_agent_job_skips_inference_resolution(tmp_path):
    original = {"id": "job1", "model": None, "provider": None, "no_agent": True}
    with patch("integration.crons.execution_model._configured_inference") as configured:
        execution = prepare_cron_hub_execution_job(original, "ops", tmp_path)
    assert execution == original
    configured.assert_not_called()


def test_empty_discovery_keeps_original_snapshots(tmp_path):
    original = {
        "id": "job1",
        "model": None,
        "provider": None,
        "model_snapshot": "old-model",
        "provider_snapshot": "old-provider",
    }
    with patch("integration.crons.execution_model._configured_inference", return_value=("", "")):
        with patch("integration.crons.execution_model._environment_model", return_value=""):
            with patch(
                "integration.crons.execution_model._discover_first_inference",
                return_value=("", ""),
            ):
                execution = prepare_cron_hub_execution_job(original, "ops", tmp_path)
    assert execution == original
