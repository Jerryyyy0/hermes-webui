"""Enrich GET /api/profiles with nested info.json, skills, and memory snapshots."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from integration.profiles.memory_snapshot import load_memory_snapshot

_log = logging.getLogger(__name__)

LOGO_MAX_BYTES = 10 * 1024 * 1024
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})
_DATA_URI_RE = re.compile(
    r"^data:(image/(?:png|jpeg|gif|webp|svg\+xml));base64,([A-Za-z0-9+/=\s]+)$",
    re.DOTALL,
)
_SVG_EVENT_ATTR_RE = re.compile(r"\son[a-z0-9_-]+\s*=", re.IGNORECASE)
_SVG_REMOTE_REF_RE = re.compile(r"(?:href|xlink:href)\s*=\s*['\"]\s*(?:https?:)?//", re.IGNORECASE)


def _read_info_json(profile_path: str) -> dict:
    path = Path(profile_path).expanduser() / "info.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _log.debug("info.json read failed for %s: %s", profile_path, exc)
        return {}


def _svg_logo_is_safe(decoded: bytes) -> bool:
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    if "<svg" not in lowered:
        return False
    if "<script" in lowered or "javascript:" in lowered:
        return False
    if _SVG_EVENT_ATTR_RE.search(text) or _SVG_REMOTE_REF_RE.search(text):
        return False
    return True


def normalize_logo_data_uri(value: str) -> str | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    mime = "image/png"
    b64_part = raw
    match = _DATA_URI_RE.match(raw)
    if match:
        mime = match.group(1)
        b64_part = match.group(2).strip()
    elif raw.startswith("data:"):
        return None

    if mime not in _ALLOWED_MIMES:
        return None

    try:
        decoded = base64.b64decode(b64_part, validate=True)
    except Exception:
        return None

    if len(decoded) > LOGO_MAX_BYTES:
        return None
    if mime == "image/svg+xml" and not _svg_logo_is_safe(decoded):
        return None

    encoded = base64.b64encode(decoded).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def read_pin_meta(profile_path: str | Path) -> tuple[bool, int | None]:
    info = _read_info_json(str(profile_path))
    pinned = info.get("pinned") is True
    if not pinned:
        return False, None
    pin_order = info.get("pin_order")
    return True, pin_order if isinstance(pin_order, int) else None


def _entry_pin_meta(entry: dict) -> tuple[bool, int | None]:
    info = entry.get("info")
    if not isinstance(info, dict):
        return False, None
    pinned = info.get("pinned") is True
    if not pinned:
        return False, None
    pin_order = info.get("pin_order")
    return True, pin_order if isinstance(pin_order, int) else None


def sort_profiles_by_pin(profiles: list) -> list:
    def sort_key(entry: dict) -> tuple:
        pinned, order = _entry_pin_meta(entry)
        if pinned:
            pin_key = order if isinstance(order, int) else 999999
            return (0, pin_key, "")
        if entry.get("is_default"):
            return (1, 0, "")
        return (2, 0, str(entry.get("name") or ""))

    return sorted(profiles, key=sort_key)


def _load_info_for_response(profile_path: str) -> dict:
    info = _read_info_json(profile_path)
    if not info:
        return {}

    out: dict = {}
    for key, val in info.items():
        if key == "logo":
            continue
        if key == "pinned":
            if val is True:
                out["pinned"] = True
            continue
        if key == "pin_order":
            if info.get("pinned") is True and isinstance(val, int):
                out["pin_order"] = val
            continue
        out[key] = val

    logo_raw = info.get("logo")
    if logo_raw:
        normalized = normalize_logo_data_uri(str(logo_raw))
        if normalized:
            out["logo"] = normalized

    return out


def _list_skills_for_profile(profile_name: str) -> list[dict]:
    try:
        from integration.skills import local_skills

        payload = local_skills.list_installed(profile_name)
        skills = payload.get("skills")
        return skills if isinstance(skills, list) else []
    except Exception as exc:
        _log.debug("skills list failed for profile %s: %s", profile_name, exc)
        return []


def enrich_profiles_response(payload: dict) -> dict:
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        return payload
    for entry in profiles:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        name = str(entry.get("name") or "")
        entry["info"] = _load_info_for_response(path)
        entry["skills"] = _list_skills_for_profile(name) if name else []
        entry["memory_snapshot"] = load_memory_snapshot(path)
    payload["profiles"] = sort_profiles_by_pin(profiles)
    return payload
