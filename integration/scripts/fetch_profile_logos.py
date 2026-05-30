#!/usr/bin/env python3
"""Download built-in profile logo presets from open-source CDNs.

Run from repo root (requires network):
  python3 integration/scripts/fetch_profile_logos.py

Sources:
  - DiceBear 9.x (MIT) — abstract gradients and role-style avatars
  - Google Noto Emoji (Apache 2.0) — emoji category
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "assets" / "profile-logos"
USER_AGENT = "hermes-webui-profile-logos/1.0"
MAX_BYTES = 100 * 1024

# (id, label, dicebear_style, seed) or (id, label, "noto", emoji_hex)
ABSTRACT = [
    ("abstract-blue-orbit", "Blue Orbit", "glass", "blue-orbit"),
    ("abstract-emerald", "Emerald", "shapes", "emerald-grove"),
    ("abstract-violet", "Violet", "rings", "violet-ring"),
    ("abstract-rose", "Rose", "identicon", "rose-bloom"),
    ("abstract-amber", "Amber", "glass", "amber-glow"),
    ("abstract-cyan", "Cyan", "shapes", "cyan-wave"),
    ("abstract-slate", "Slate", "rings", "slate-stone"),
    ("abstract-indigo", "Indigo", "identicon", "indigo-deep"),
]

ROLES = [
    ("roles-bot", "Bot", "bottts-neutral", "hermes-bot"),
    ("roles-code", "Code", "notionists", "code-assistant"),
    ("roles-terminal", "Terminal", "pixel-art", "terminal-shell"),
    ("roles-settings", "Settings", "lorelei", "settings-engine"),
    ("roles-user", "User", "avataaars", "human-user"),
    ("roles-brain", "Brain", "notionists", "think-brain"),
    ("roles-sparkles", "Sparkles", "fun-emoji", "magic-sparkles"),
    ("roles-chart", "Chart", "thumbs", "data-chart"),
    ("roles-shield", "Shield", "lorelei", "security-shield"),
    ("roles-search", "Search", "notionists", "search-agent"),
    ("roles-pencil", "Pencil", "avataaars", "writer-pencil"),
    ("roles-database", "Database", "bottts-neutral", "data-store"),
]

EMOJI = [
    ("emoji-robot", "Robot", "noto", "1f916"),
    ("emoji-rocket", "Rocket", "noto", "1f680"),
    ("emoji-gear", "Gear", "noto", "2699"),
    ("emoji-lightning", "Lightning", "noto", "26a1"),
]

DICEBEAR_BASE = "https://api.dicebear.com/9.x"
NOTO_EMOJI_BASE = "https://fonts.gstatic.com/s/e/notoemoji/latest"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if len(data) > MAX_BYTES:
        raise ValueError(f"asset too large ({len(data)} bytes): {url}")
    if len(data) < 64:
        raise ValueError(f"asset too small ({len(data)} bytes): {url}")
    return data


def _dicebear_url(style: str, seed: str) -> str:
    from urllib.parse import quote

    return f"{DICEBEAR_BASE}/{quote(style)}/png?seed={quote(seed)}&size=128"


def _noto_url(codepoint: str) -> str:
    return f"{NOTO_EMOJI_BASE}/{codepoint}/512.png"


def _write_preset(
    presets: list[dict],
    *,
    preset_id: str,
    label: str,
    category: str,
    rel: str,
    url: str,
    license_id: str,
) -> None:
    data = _fetch(url)
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    presets.append(
        {
            "id": preset_id,
            "category": category,
            "label": label,
            "file": rel,
            "license": license_id,
        }
    )


def main() -> int:
    presets: list[dict] = []
    errors: list[str] = []

    for preset_id, label, style, seed in ABSTRACT:
        rel = f"abstract/{preset_id}.png"
        url = _dicebear_url(style, seed)
        try:
            _write_preset(
                presets,
                preset_id=preset_id,
                label=label,
                category="abstract",
                rel=rel,
                url=url,
                license_id="dicebear-9-mit",
            )
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errors.append(f"{preset_id}: {exc}")

    for preset_id, label, style, seed in ROLES:
        rel = f"roles/{preset_id}.png"
        url = _dicebear_url(style, seed)
        try:
            _write_preset(
                presets,
                preset_id=preset_id,
                label=label,
                category="roles",
                rel=rel,
                url=url,
                license_id="dicebear-9-mit",
            )
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errors.append(f"{preset_id}: {exc}")

    for preset_id, label, source, codepoint in EMOJI:
        if source != "noto":
            errors.append(f"{preset_id}: unsupported source {source}")
            continue
        rel = f"emoji/{preset_id}.png"
        url = _noto_url(codepoint)
        try:
            _write_preset(
                presets,
                preset_id=preset_id,
                label=label,
                category="emoji",
                rel=rel,
                url=url,
                license_id="noto-emoji-apache-2.0",
            )
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errors.append(f"{preset_id}: {exc}")

    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    manifest = {
        "categories": [
            {"id": "abstract", "label": "Abstract"},
            {"id": "roles", "label": "Roles"},
            {"id": "emoji", "label": "Emoji"},
        ],
        "presets": presets,
    }
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    licenses = """# Profile logo presets licenses

Regenerate with `python3 integration/scripts/fetch_profile_logos.py` (network required).

## DiceBear 9.x (abstract + roles)

- **License:** MIT — https://github.com/dicebear/dicebear/blob/main/LICENSE
- **Source:** https://api.dicebear.com/9.x/
- **Files:** `abstract/*.png`, `roles/*.png`

## Google Noto Emoji (emoji category)

- **License:** Apache 2.0 — https://fonts.google.com/noto/specimen/Noto+Emoji
- **Source:** https://fonts.gstatic.com/s/e/notoemoji/latest/
- **Files:** `emoji/*.png`
"""
    (ROOT / "LICENSES.md").write_text(licenses, encoding="utf-8")
    print(f"Wrote {len(presets)} presets under {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
