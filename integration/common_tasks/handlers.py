"""HTTP handler for common tasks reads.

GET /api/integration/common_tasks?profile=xxx
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import bad, j
from integration.common_tasks import collectors, generation, store
from integration.config import integration_enabled


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path != "/api/integration/common_tasks":
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
    tasks, _state = store.read_all(profile_path)
    generation.enqueue_missing_or_stale(profile, profile_path)
    items = store.pick_top3(tasks)
    if any(t["source"] == "mined" for t in items):
        cache_status = "hit"
    elif any(t["source"] == "seed" for t in items):
        cache_status = "seed"
    else:
        cache_status = "empty"
    j(handler, {"profile": profile, "items": items, "cache_status": cache_status})
    return True
