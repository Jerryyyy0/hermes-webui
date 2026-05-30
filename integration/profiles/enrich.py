"""Enrich GET /api/profiles with nested info.json, skills, and memory snapshots."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from integration.profiles.memory_snapshot import load_memory_snapshot

_log = logging.getLogger(__name__)

LOGO_MAX_BYTES = 100 * 1024
_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_DATA_URI_RE = re.compile(
    r"^data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$",
    re.DOTALL,
)


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

    encoded = base64.b64encode(decoded).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _load_info_for_response(profile_path: str) -> dict:
    info = _read_info_json(profile_path)
    if not info:
        return {}

    out: dict = {}
    for key, val in info.items():
        if key == "logo":
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
    return payload
