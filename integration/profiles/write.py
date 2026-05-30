"""Write profile info.json (display_name, description, logo base64)."""

from __future__ import annotations

import json
from pathlib import Path

from integration.profiles.enrich import _load_info_for_response, normalize_logo_data_uri
from integration.profiles.presets import preset_logo_data_uri


def _resolve_profile_path(name: str) -> Path:
    from api.profiles import list_profiles_api

    for p in list_profiles_api():
        if p.get("name") == name:
            path = p.get("path")
            if path:
                return Path(str(path)).expanduser()
    raise FileNotFoundError(f"Profile '{name}' not found")


def _read_info_file(profile_path: Path) -> dict:
    info_path = profile_path / "info.json"
    if not info_path.is_file():
        return {}
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_info_file(profile_path: Path, info: dict) -> None:
    profile_path.mkdir(parents=True, exist_ok=True)
    info_path = profile_path / "info.json"
    tmp_path = profile_path / "info.json.tmp"
    tmp_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(info_path)


def _enriched_profile_entry(name: str, profile_path: Path) -> dict:
    from api.profiles import get_active_profile_name, list_profiles_api

    active = get_active_profile_name()
    base: dict = {
        "name": name,
        "path": str(profile_path),
    }
    for p in list_profiles_api():
        if p.get("name") == name:
            base = dict(p)
            break
    base["info"] = _load_info_for_response(str(profile_path))
    try:
        from integration.skills import local_skills

        payload = local_skills.list_installed(name)
        base["skills"] = payload.get("skills") or []
    except Exception:
        base["skills"] = []
    base["is_active"] = name == active
    return base


def save_profile_info(name: str, fields: dict) -> dict:
    from api.profiles import _validate_profile_name

    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    if name != "default":
        _validate_profile_name(name)

    profile_path = _resolve_profile_path(name)
    info = _read_info_file(profile_path)

    if "display_name" in fields:
        info["display_name"] = str(fields.get("display_name") or "")
    if "description" in fields:
        info["description"] = str(fields.get("description") or "")

    logo_preset = fields.get("logo_preset")
    logo_base64 = fields.get("logo_base64")
    remove_logo = fields.get("remove_logo")

    has_preset = logo_preset is not None and str(logo_preset).strip() != ""
    has_upload = logo_base64 is not None and str(logo_base64).strip() != ""
    if has_preset and has_upload:
        raise ValueError("logo_preset and logo_base64 are mutually exclusive")

    if remove_logo:
        info.pop("logo", None)
    elif has_preset:
        data_uri = preset_logo_data_uri(str(logo_preset).strip())
        if not data_uri:
            raise ValueError(f"Unknown or invalid logo preset: {logo_preset}")
        info["logo"] = data_uri
    elif has_upload:
        data_uri = normalize_logo_data_uri(str(logo_base64).strip())
        if not data_uri:
            raise ValueError("Invalid logo_base64")
        info["logo"] = data_uri
    elif logo_base64 is not None and not str(logo_base64).strip():
        info.pop("logo", None)

    if info:
        _write_info_file(profile_path, info)
    else:
        info_path = profile_path / "info.json"
        if info_path.is_file():
            info_path.unlink()

    return {"ok": True, "profile": _enriched_profile_entry(name, profile_path)}
