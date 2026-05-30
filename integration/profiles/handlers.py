"""HTTP handlers for profile info and logo presets."""

from __future__ import annotations

from urllib.parse import parse_qs

from api.helpers import bad, j

from integration.config import integration_enabled
from integration.profiles.presets import list_logo_presets
from integration.profiles.write import save_profile_info


def try_handle_get(handler, parsed) -> bool:
    if not integration_enabled():
        return False
    if parsed.path == "/api/profile/logo-presets":
        qs = parse_qs(parsed.query or "")
        category = (qs.get("category") or [""])[0]
        payload = list_logo_presets(category or None)
        j(handler, payload)
        return True
    return False


def try_handle_post(handler, parsed, body: dict | None) -> bool:
    if not integration_enabled():
        return False
    body = body if isinstance(body, dict) else {}
    if parsed.path == "/api/profile/info":
        name = str(body.get("name") or "").strip()
        if not name:
            bad(handler, "name is required")
            return True
        fields = {}
        for key in ("display_name", "description", "logo_preset", "logo_base64", "remove_logo"):
            if key in body:
                fields[key] = body[key]
        try:
            result = save_profile_info(name, fields)
            j(handler, result)
        except FileNotFoundError as exc:
            bad(handler, str(exc), 404)
        except ValueError as exc:
            bad(handler, str(exc), 400)
        return True
    return False
