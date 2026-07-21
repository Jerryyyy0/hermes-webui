"""Collectors for assistant bubble generation context."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

PROMPT_VERSIONS = {
    "assistant_intro": "assistant_intro.v1",
    "memory": "memory.v1",
    "skill": "skill.v3",
    "emotion": "emotion.v3",
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


def _disabled_skills(profile_path: Path) -> set[str]:
    cfg_path = Path(profile_path) / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    except Exception:
        return set()
    if not isinstance(cfg, dict):
        return set()
    skills_cfg = cfg.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()
    disabled = skills_cfg.get("disabled")
    platform_disabled = skills_cfg.get("platform_disabled")
    if isinstance(platform_disabled, dict) and "webui" in platform_disabled:
        disabled = platform_disabled.get("webui")
    if isinstance(disabled, str):
        disabled = [disabled]
    if not isinstance(disabled, list):
        return set()
    return {str(item).strip() for item in disabled if str(item).strip()}


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                meta = {}
            return (meta if isinstance(meta, dict) else {}, parts[2])
    return {}, text


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _read_skill_detail(skill_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((skill_dir / ".detail.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_skills(profile_path: Path) -> list[dict[str, str]]:
    skills_dir = Path(profile_path) / "skills"
    disabled = _disabled_skills(profile_path)
    if not skills_dir.is_dir():
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        skill_dir = skill_md.parent
        meta, body = _parse_skill_frontmatter(text)
        name = str(meta.get("name") or skill_dir.name).strip()
        if not name or name in seen or name in disabled:
            continue
        detail = _read_skill_detail(skill_dir)
        display_name = str(detail.get("display_name") or meta.get("display_name") or "").strip()
        desc = str(
            detail.get("display_description")
            or meta.get("display_description")
            or meta.get("description")
            or ""
        ).strip()
        if not desc:
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    desc = line
                    break
        label = display_name if _has_cjk(display_name) else ""
        if not label and _has_cjk(name):
            label = name
        if not label and not _has_cjk(desc):
            seen.add(name)
            continue
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
        skills = collect_skills(profile_path)
        context["skills"] = skills
        context["skills_count"] = len(skills)
    return context


def fingerprint_for(category: str, context: dict[str, Any]) -> str:
    payload = {"category": category, "prompt_version": PROMPT_VERSIONS[category], "context": context}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def scheduled_task_stats(profile_path: Path) -> dict[str, int]:
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
        state = str(job.get("state") or "").lower()
        last_status = str(job.get("last_status") or "").lower()
        status = str(job.get("status") or "").lower()
        has_error = bool(job.get("last_error") or job.get("last_delivery_error"))
        if status in {"running", "in_progress", "executing"} or state in {"running", "in_progress", "executing"}:
            running += 1
        elif has_error or last_status in {"failed", "error", "errored"} or state in {"failed", "error", "errored"}:
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
    if isinstance(model_cfg, str):
        model = model_cfg.strip() or None
    elif isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip() or None
        model = str(model_cfg.get("default") or model_cfg.get("name") or "").strip() or None
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
    return {"provider": provider, "model": model, "base_url": base_url}


def skills_block(skills: list[dict[str, str]]) -> str:
    if not skills:
        return ""
    lines = []
    for skill in skills:
        label = str(skill.get("label") or "").strip()
        desc = str(skill.get("description") or "").strip()
        if not label and not _has_cjk(desc):
            continue
        if label:
            lines.append(f"- {label}：{desc}" if desc else f"- {label}")
        elif desc:
            lines.append(f"- {desc}")
    return "\n".join(lines)


def sanitize_for_prompt(value: Any, max_len: int = 1000) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[:max_len]
