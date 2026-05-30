"""Built-in profile logo presets (local assets)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from integration.profiles.enrich import normalize_logo_data_uri

_PRESETS_ROOT = Path(__file__).resolve().parent.parent / "assets" / "profile-logos"
_MANIFEST_PATH = _PRESETS_ROOT / "manifest.json"


def _load_manifest() -> dict:
    if not _MANIFEST_PATH.is_file():
        return {"categories": [], "presets": []}
    try:
        data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"categories": [], "presets": []}
    except Exception:
        return {"categories": [], "presets": []}


def list_logo_presets(category: str | None = None) -> dict:
    manifest = _load_manifest()
    categories = manifest.get("categories")
    presets_raw = manifest.get("presets")
    if not isinstance(categories, list):
        categories = []
    if not isinstance(presets_raw, list):
        presets_raw = []

    category_key = str(category or "").strip()
    presets = []
    for item in presets_raw:
        if not isinstance(item, dict):
            continue
        preset_id = str(item.get("id") or "").strip()
        file_rel = str(item.get("file") or "").strip()
        if not preset_id or not file_rel:
            continue
        cat = str(item.get("category") or "").strip()
        if category_key and cat != category_key:
            continue
        url_path = "/static/integration/profile-logos/" + file_rel.lstrip("/")
        presets.append(
            {
                "id": preset_id,
                "category": cat,
                "label": str(item.get("label") or preset_id),
                "url": url_path,
            }
        )

    return {"categories": categories, "presets": presets}


def preset_logo_data_uri(preset_id: str) -> str | None:
    preset_id = str(preset_id or "").strip()
    if not preset_id:
        return None
    manifest = _load_manifest()
    presets_raw = manifest.get("presets")
    if not isinstance(presets_raw, list):
        return None

    file_rel = ""
    for item in presets_raw:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == preset_id:
            file_rel = str(item.get("file") or "").strip()
            break
    if not file_rel:
        return None

    file_path = (_PRESETS_ROOT / file_rel).resolve()
    try:
        file_path.relative_to(_PRESETS_ROOT.resolve())
    except ValueError:
        return None
    if not file_path.is_file():
        return None

    ext = file_path.suffix.lower().lstrip(".") or "png"
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext, "image/png")
    raw = file_path.read_bytes()
    if len(raw) > 100 * 1024:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return normalize_logo_data_uri(f"data:{mime};base64,{b64}")
