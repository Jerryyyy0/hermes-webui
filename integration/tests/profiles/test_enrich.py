"""Profile enrich tests."""

import base64
import json
from pathlib import Path
from unittest.mock import patch

from integration.profiles.enrich import LOGO_MAX_BYTES, enrich_profiles_response, normalize_logo_data_uri
from integration.profiles.memory_snapshot import load_memory_snapshot

_FAKE_SK_KEY = "sk-TestFakeOpenAIKey1234567890abcdef"


def _enrich(payload, *, skills=None):
    skills = skills if skills is not None else []
    with patch("integration.profiles.enrich._list_skills_for_profile", return_value=skills):
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
    assert entry["skills"] == []
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
    mock_skills = [{"name": "demo-skill", "description": "d", "disabled": False}]
    out = _enrich(payload, skills=mock_skills)
    entry = out["profiles"][0]
    assert entry["info"] == {}
    assert entry["skills"] == mock_skills


def _write_memory_files(profile_dir: Path) -> None:
    mem_dir = profile_dir / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("agent notes", encoding="utf-8")
    (mem_dir / "USER.md").write_text("user profile", encoding="utf-8")
    (profile_dir / "SOUL.md").write_text("agent soul", encoding="utf-8")


def test_enrich_memory_snapshot_all_files(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    _write_memory_files(profile_dir)
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    snap = out["profiles"][0]["memory_snapshot"]
    assert snap["memory"] == "agent notes"
    assert snap["user"] == "user profile"
    assert snap["soul"] == "agent soul"
    assert snap["memory_path"].endswith("MEMORY.md")
    assert snap["user_path"].endswith("USER.md")
    assert snap["soul_path"].endswith("SOUL.md")
    assert snap["memory_mtime"] is not None
    assert snap["user_mtime"] is not None
    assert snap["soul_mtime"] is not None


def test_enrich_memory_snapshot_missing_files(tmp_path):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    payload = {"profiles": [{"name": "p1", "path": str(profile_dir)}], "active": "p1"}
    out = _enrich(payload)
    snap = out["profiles"][0]["memory_snapshot"]
    assert snap["memory"] == ""
    assert snap["user"] == ""
    assert snap["soul"] == ""
    assert snap["memory_mtime"] is None
    assert snap["user_mtime"] is None
    assert snap["soul_mtime"] is None


def test_enrich_memory_snapshot_empty_path():
    payload = {"profiles": [{"name": "p1", "path": ""}], "active": "p1"}
    out = _enrich(payload)
    snap = out["profiles"][0]["memory_snapshot"]
    assert snap["memory"] == ""
    assert snap["memory_path"] == ""
    assert snap["memory_mtime"] is None


def test_load_memory_snapshot_redacts_credentials(tmp_path, monkeypatch):
    profile_dir = tmp_path / "p1"
    profile_dir.mkdir()
    mem_dir = profile_dir / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text(f"key={_FAKE_SK_KEY}", encoding="utf-8")

    monkeypatch.setattr(
        "api.config.load_settings",
        lambda: {"api_redact_enabled": True},
    )
    snap = load_memory_snapshot(str(profile_dir))
    assert _FAKE_SK_KEY not in snap["memory"]
    assert snap["memory"]


def test_normalize_logo_data_uri_raw_base64():
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    out = normalize_logo_data_uri(b64)
    assert out and out.startswith("data:image/png;base64,")
