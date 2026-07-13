"""Execution-only inference fallback for Cron Hub manual runs."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path

from api.config import get_available_models, get_config_for_profile_home
from api.profiles import (
    clear_request_profile,
    get_active_profile_name,
    get_profile_runtime_env,
    set_request_profile,
)

logger = logging.getLogger(__name__)


def _configured_inference(profile_home: Path) -> tuple[str, str]:
    config = get_config_for_profile_home(profile_home)
    model_config = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_config, str):
        return model_config.strip(), ""
    if isinstance(model_config, dict):
        model = model_config.get("default") or model_config.get("model")
        provider = model_config.get("provider")
        return (
            model.strip() if isinstance(model, str) else "",
            provider.strip() if isinstance(provider, str) else "",
        )
    return "", ""


def _environment_model(profile_home: Path) -> str:
    runtime_env = get_profile_runtime_env(profile_home)
    for name in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        model = runtime_env.get(name) or os.getenv(name)
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""


def _first_catalog_inference(catalog: dict) -> tuple[str, str]:
    groups = catalog.get("groups") if isinstance(catalog, dict) else None
    if not isinstance(groups, list):
        return "", ""
    for group in groups:
        if not isinstance(group, dict):
            continue
        provider = group.get("provider_id") or group.get("provider")
        provider = provider.strip() if isinstance(provider, str) else ""
        for field in ("models", "extra_models"):
            entries = group.get(field)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                model_id = entry.get("id") if isinstance(entry, dict) else entry
                if isinstance(model_id, str) and model_id.strip():
                    return model_id.strip(), provider
    return "", ""


def _discover_first_inference(profile: str) -> tuple[str, str]:
    previous_profile = get_active_profile_name()
    set_request_profile(profile)
    try:
        cached = get_available_models(prefer_cache=True)
        inference = _first_catalog_inference(cached)
        if inference[0]:
            return inference
        return _first_catalog_inference(get_available_models())
    except Exception:
        logger.warning(
            "Cron Hub could not discover fallback inference for profile %s",
            profile,
            exc_info=True,
        )
        return "", ""
    finally:
        if previous_profile:
            set_request_profile(previous_profile)
        else:
            clear_request_profile()


def prepare_cron_hub_execution_job(job: dict, profile: str, profile_home: Path) -> dict:
    """Return a detached copy that runs an unpinned job on current inference."""
    execution_job = copy.deepcopy(job)
    if execution_job.get("no_agent") is True:
        return execution_job

    explicit_model = str(execution_job.get("model") or "").strip()
    explicit_provider = str(execution_job.get("provider") or "").strip()
    if explicit_model or explicit_provider:
        return execution_job

    profile_home = Path(profile_home)
    model, provider = _configured_inference(profile_home)
    if not model:
        model = _environment_model(profile_home)
    if not model:
        model, discovered_provider = _discover_first_inference(profile)
        if not provider:
            provider = discovered_provider

    if not model and profile != "default":
        from api.profiles import get_hermes_home_for_profile

        default_home = get_hermes_home_for_profile("default")
        model, default_provider = _configured_inference(default_home)
        if not model:
            model = _environment_model(default_home)
        if not model:
            model, discovered_provider = _discover_first_inference("default")
            if not default_provider:
                default_provider = discovered_provider
        if not provider:
            provider = default_provider

    if not model:
        return execution_job

    execution_job["model"] = model
    if provider:
        execution_job["provider"] = provider
    execution_job["model_snapshot"] = None
    execution_job["provider_snapshot"] = None
    return execution_job
