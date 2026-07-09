"""Static checks for profile logo upload UI."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROFILES_JS = ROOT / "integration" / "assets" / "hermes_profiles.js"


def test_profile_logo_upload_accepts_svg_and_uses_ten_mb_limit():
    src = PROFILES_JS.read_text(encoding="utf-8")
    assert "const LOGO_MAX_BYTES = 10 * 1024 * 1024;" in src
    assert 'accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"' in src
    assert "Logo must be 10MB or smaller" in src
