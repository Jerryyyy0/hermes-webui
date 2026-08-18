"""HTTP handler for assistant bubble reads."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import bad, j
from integration.assistant_bubbles import collectors, copy, generation, store
from integration.config import integration_enabled


def _fallback_items(profile: str, profile_path: Path) -> list[dict]:
    intro_context = collectors.collect_context(profile, profile_path, "assistant_intro")
    memory_context = collectors.collect_context(profile, profile_path, "memory")
    skill_context = collectors.collect_context(profile, profile_path, "skill")
    emotion_context = collectors.collect_context(profile, profile_path, "emotion")
    emotions = copy.fallback_emotions(emotion_context)
    stats = collectors.scheduled_task_stats(profile_path)
    return [
        {"type": "assistant_intro", "text": copy.fallback_text("assistant_intro", intro_context)},
        {"type": "emotion", "text": emotions[0]},
        {"type": "scheduled_task", "text": copy.scheduled_task_text(stats)},
        {"type": "emotion", "text": emotions[1]},
        {"type": "memory", "text": copy.fallback_text("memory", memory_context)},
        {"type": "emotion", "text": emotions[2]},
        {"type": "skill", "text": copy.fallback_text("skill", skill_context)},
        {"type": "emotion", "text": emotions[3]},
    ]


def _items_for_response(profile: str, profile_path: Path, cache: dict | None) -> list[dict]:
    if not cache:
        return _fallback_items(profile, profile_path)
    cached_items = cache.get("items")
    if not isinstance(cached_items, list) or len(cached_items) != len(store.ITEM_ORDER):
        return _fallback_items(profile, profile_path)
    out: list[dict] = []
    fallback: list[dict] | None = None
    stats = collectors.scheduled_task_stats(profile_path)
    for idx, item in enumerate(cached_items):
        if not isinstance(item, dict) or item.get("type") != store.ITEM_ORDER[idx]:
            return _fallback_items(profile, profile_path)
        item_type = item["type"]
        if item_type == "scheduled_task":
            out.append({"type": "scheduled_task", "text": copy.scheduled_task_text(stats)})
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            if fallback is None:
                fallback = _fallback_items(profile, profile_path)
            out.append(fallback[idx])
            continue
        out.append({"type": item_type, "text": text})
    return out


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path != "/api/integration/assistant_bubbles":
        return False
    qs = parse_qs(parsed.query or "")
    profile = str((qs.get("profile") or [""])[0] or "").strip()
    if not profile:
        bad(handler, "profile 为必填参数", 400)
        return True
    row = collectors.resolve_profile(profile)
    if not row:
        bad(handler, "Profile 不存在", 404)
        return True
    profile_path = Path(row["path"])
    cache = store.read_store(profile_path)
    generation.enqueue_missing_or_stale(profile, profile_path, cache)
    payload = {
        "profile": profile,
        "items": _items_for_response(profile, profile_path, cache),
        "cache_status": "hit" if cache else "fallback",
    }
    j(handler, payload)
    return True
