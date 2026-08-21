"""Collectors for assistant bubble generation context."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

PROMPT_VERSIONS = {
    "assistant_intro": "assistant_intro.v3",
    "memory": "memory.v3",
    "skill": "skill.v6",
    "emotion": "emotion.v6",
}


def resolve_profile(profile_name: str) -> dict[str, Any] | None:
    from api.profiles import list_profiles_api

    needle = str(profile_name or "").strip()
    if not needle:
        return None
    for row in list_profiles_api():
        if str(row.get("name") or "").strip() == needle:
            path = row.get("path")
            if path:
                return {**row, "path": str(Path(path))}
    return None


def load_info(profile_path: Path) -> dict[str, Any]:
    path = Path(profile_path) / "info.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_limited(path: Path, limit: int = 4000) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        pass
    return ""


def _latest_memory(memory_text: str) -> str:
    lines = [line.strip(" -\t") for line in memory_text.splitlines()]
    values = [line for line in lines if line and not line.startswith("#")]
    return values[-1][:500] if values else ""


def collect_skills(profile_name: str) -> list[dict[str, str]]:
    """Installed hub + custom skills for the profile, excluding disabled (local_all)."""
    from integration.skills.listing import list_local_all_enabled_skills

    raw = list_local_all_enabled_skills(profile_name)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for skill in raw:
        if not isinstance(skill, dict):
            continue
        name = str(skill.get("name") or "").strip()
        if not name or name in seen:
            continue
        label = str(skill.get("display_name") or name).strip() or name
        desc = str(
            skill.get("display_description") or skill.get("description") or ""
        ).strip()
        seen.add(name)
        out.append({"name": name, "label": label, "description": desc})
    return out


def collect_context(profile_name: str, profile_path: Path, category: str) -> dict[str, Any]:
    info = load_info(profile_path)
    display_name = str(info.get("display_name") or profile_name or "Hermes").strip()
    description = str(info.get("description") or "").strip()
    soul = _read_limited(Path(profile_path) / "SOUL.md")
    context: dict[str, Any] = {
        "profile": profile_name,
        "profile_path": str(profile_path),
        "display_name": display_name,
        "description": description,
        "soul": soul,
    }
    if category == "memory":
        memory = _read_limited(Path(profile_path) / "memories" / "MEMORY.md")
        context["latest_memory"] = _latest_memory(memory)
    if category == "skill":
        skills = collect_skills(profile_name)
        context["skills"] = skills
        context["skills_count"] = len(skills)
    return context


def fingerprint_for(category: str, context: dict[str, Any]) -> str:
    payload = {"category": category, "prompt_version": PROMPT_VERSIONS[category], "context": context}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scheduled_task_stats(profile_path: Path) -> dict[str, int]:
    # Keep the bubble's summary aligned with the Cron API. In particular,
    # manually triggered jobs are tracked in the WebUI process rather than by
    # mutating jobs.json while they run.
    from api.routes import _cron_job_for_api

    jobs_path = Path(profile_path) / "cron" / "jobs.json"
    try:
        payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"total": 0, "running": 0, "pending": 0, "failed": 0}
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        jobs = []
    total = running = pending = failed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        total += 1
        execution_bucket = _cron_job_for_api(job).get("execution_bucket")
        if execution_bucket == "running":
            running += 1
        elif execution_bucket == "error":
            failed += 1
        else:
            pending += 1
    return {"total": total, "running": running, "pending": pending, "failed": failed}


def model_route(profile_path: Path) -> dict[str, str | None]:
    cfg_path = Path(profile_path) / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    model_cfg = cfg.get("model")
    provider = None
    model = None
    configured_provider = None
    model_base_url = None
    if isinstance(model_cfg, str):
        model = model_cfg.strip() or None
    elif isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip() or None
        configured_provider = provider
        model = str(model_cfg.get("default") or model_cfg.get("name") or "").strip() or None
        model_base_url = str(model_cfg.get("base_url") or "").strip() or None
    try:
        from api.profiles import _split_webui_provider_model_value

        model, provider = _split_webui_provider_model_value(model, provider)
    except Exception:
        pass
    base_url = None
    providers_cfg = cfg.get("providers")
    if provider and isinstance(providers_cfg, dict):
        provider_cfg = providers_cfg.get(provider)
        if isinstance(provider_cfg, dict):
            base_url = str(provider_cfg.get("base_url") or "").strip() or None
    if not base_url and model_base_url:
        if provider is None:
            # Legacy configs with only model.base_url describe an
            # OpenAI-compatible custom endpoint, not the first-party provider
            # inferred from the model name by the auxiliary auto chain.
            provider = "custom"
            base_url = model_base_url
        elif configured_provider and provider == configured_provider:
            # Keep named custom-provider resolution inside Hermes Agent. It
            # may need the custom_providers entry's api_mode/base_url rather
            # than an explicit URL passed by this integration layer.
            custom_providers = cfg.get("custom_providers")
            is_named_custom = provider == "custom" or provider.startswith("custom:")
            if not (is_named_custom and isinstance(custom_providers, list) and custom_providers):
                base_url = model_base_url
    return {"provider": provider, "model": model, "base_url": base_url}


def skills_block(skills: list[dict[str, str]]) -> str:
    if not skills:
        return ""
    lines = []
    for skill in skills:
        label = str(skill.get("label") or "").strip()
        desc = str(skill.get("description") or "").strip()
        name = str(skill.get("name") or "").strip()
        display = label or name
        if display:
            lines.append(f"- {display}：{desc}" if desc else f"- {display}")
        elif desc:
            lines.append(f"- {desc}")
    return "\n".join(lines)


def sanitize_for_prompt(value: Any, max_len: int = 1000) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[:max_len]
