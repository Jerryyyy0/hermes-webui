"""Profile resolution and skills directory paths (delegates to api.profiles)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from api.helpers import get_profile_cookie
from api.profiles import get_hermes_home_for_profile


def resolve_profile_name(query: str, cookie_profile: str | None) -> str:
    qs = parse_qs(query or "")
    raw = (qs.get("profile") or [""])[0].strip()
    if raw:
        return raw
    if cookie_profile:
        return cookie_profile
    return "default"


def resolve_profile_from_request(handler, parsed) -> str:
    return resolve_profile_name(parsed.query or "", get_profile_cookie(handler))


def resolve_active_profile_from_cookie(handler) -> str:
    """Active WebUI profile from cookie only (reserved for non-SkillHub integration)."""
    cookie = get_profile_cookie(handler)
    if cookie:
        return cookie.strip()
    return "default"


def skills_dir_for_profile(profile_name: str) -> Path:
    return Path(get_hermes_home_for_profile(profile_name)) / "skills"


def shared_skills_dir() -> Path:
    """Base ``{HERMES_HOME}/skills`` — not under ``profiles/<name>/``."""
    return skills_dir_for_profile("default")
