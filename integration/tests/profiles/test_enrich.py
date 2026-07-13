"""Profile enrich tests."""

import base64
import json
from integration.profiles.enrich import LOGO_MAX_BYTES, enrich_profiles_response, normalize_logo_data_uri


def _enrich(payload):
    return enrich_profiles_response(payload)

# Minimal valid 1x1 PNG
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _tiny_data_uri() -> str:
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    return f"data:image/png;base64,{b64}"


def test_enrich_nested_info(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    (profile_dir / "info.json").write_text(
        json.dumps({"display_name": "Demo", "description": "A profile"}),
        encoding="utf-8",
    )
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    entry = out["profiles"][0]
    assert entry["info"]["display_name"] == "Demo"
    assert entry["info"]["description"] == "A profile"
    assert "logo" not in entry["info"]
    assert "skills" not in entry
    assert "memory_snapshot" not in entry
    assert "logo_base64" not in entry
    assert "display_name" not in entry


def test_enrich_logo_in_info(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    uri = _tiny_data_uri()
    (profile_dir / "info.json").write_text(json.dumps({"logo": uri}), encoding="utf-8")
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    assert out["profiles"][0]["info"]["logo"].startswith("data:image/png;base64,")


def test_enrich_invalid_logo_path_omitted(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    (profile_dir / "info.json").write_text(json.dumps({"logo": "/tmp/missing.png"}), encoding="utf-8")
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    assert "logo" not in out["profiles"][0]["info"]


def test_enrich_logo_oversized_omitted(tmp_path):
    big = base64.b64encode(b"\x00" * (LOGO_MAX_BYTES + 1)).decode("ascii")
    uri = f"data:image/png;base64,{big}"
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    (profile_dir / "info.json").write_text(json.dumps({"logo": uri}), encoding="utf-8")
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    assert "logo" not in out["profiles"][0]["info"]


def test_enrich_missing_info_json(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    entry = out["profiles"][0]
    assert entry["info"] == {}
    assert "skills" not in entry
    assert "memory_snapshot" not in entry


def test_normalize_logo_data_uri_raw_base64():
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    out = normalize_logo_data_uri(b64)
    assert out and out.startswith("data:image/png;base64,")


def test_normalize_logo_data_uri_accepts_safe_svg():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
    b64 = base64.b64encode(svg).decode("ascii")
    out = normalize_logo_data_uri(f"data:image/svg+xml;base64,{b64}")
    assert out and out.startswith("data:image/svg+xml;base64,")


def test_normalize_logo_data_uri_rejects_script_svg():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    b64 = base64.b64encode(svg).decode("ascii")
    assert normalize_logo_data_uri(f"data:image/svg+xml;base64,{b64}") is None


def test_normalize_logo_data_uri_rejects_svg_event_attrs():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
    b64 = base64.b64encode(svg).decode("ascii")
    assert normalize_logo_data_uri(f"data:image/svg+xml;base64,{b64}") is None


def test_normalize_logo_data_uri_rejects_svg_remote_refs():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>'
    b64 = base64.b64encode(svg).decode("ascii")
    assert normalize_logo_data_uri(f"data:image/svg+xml;base64,{b64}") is None


def test_normalize_logo_data_uri_accepts_ten_mb_limit():
    raw = b"a" * LOGO_MAX_BYTES
    b64 = base64.b64encode(raw).decode("ascii")
    out = normalize_logo_data_uri(f"data:image/png;base64,{b64}")
    assert out and out.startswith("data:image/png;base64,")
