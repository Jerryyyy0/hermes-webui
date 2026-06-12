"""Profile pin tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from integration.profiles.enrich import enrich_profiles_response
from integration.profiles.pin import PROFILE_PIN_LIMIT, set_profile_pinned


def _profile_row(name: str, path: Path, *, is_default: bool = False) -> dict:
    return {
        "name": name,
        "path": str(path),
        "is_default": is_default,
        "is_active": False,
        "gateway_running": False,
        "model": None,
        "provider": None,
        "has_env": False,
        "visible": True,
        "skill_count": 0,
        "enabled_skills": 0,
        "total_skills": 0,
    }


def _fake_profiles(tmp_path: Path) -> list[dict]:
    default_home = tmp_path / "default"
    default_home.mkdir()
    alpha = tmp_path / "profiles" / "alpha"
    beta = tmp_path / "profiles" / "beta"
    gamma = tmp_path / "profiles" / "gamma"
    for path in (alpha, beta, gamma):
        path.mkdir(parents=True)
    return [
        _profile_row("default", default_home, is_default=True),
        _profile_row("alpha", alpha),
        _profile_row("beta", beta),
        _profile_row("gamma", gamma),
    ]


def _profile_patches(rows):
    return (
        patch("api.profiles.list_profiles_api", return_value=rows),
        patch("api.profiles.get_active_profile_name", return_value="default"),
        patch("integration.skills.local_skills.list_installed", return_value={"skills": []}),
    )


def test_pin_and_unpin_writes_info_json(tmp_path):
    rows = _fake_profiles(tmp_path)
    target = next(r for r in rows if r["name"] == "alpha")

    with _profile_patches(rows)[0], _profile_patches(rows)[1], _profile_patches(rows)[2]:
        out = set_profile_pinned("alpha", True)

    info_path = Path(target["path"]) / "info.json"
    data = json.loads(info_path.read_text(encoding="utf-8"))
    assert data["pinned"] is True
    assert data["pin_order"] == 1
    assert out["ok"] is True
    assert out["profile"]["info"]["pinned"] is True
    assert out["profile"]["info"]["pin_order"] == 1
    assert "pinned" not in out["profile"]
    assert "pin_order" not in out["profile"]

    with _profile_patches(rows)[0], _profile_patches(rows)[1], _profile_patches(rows)[2]:
        set_profile_pinned("alpha", False)

    assert not info_path.exists()


def test_enrich_sorts_default_then_pinned_then_alpha(tmp_path):
    rows = _fake_profiles(tmp_path)
    beta = next(r for r in rows if r["name"] == "beta")
    gamma = next(r for r in rows if r["name"] == "gamma")
    (Path(beta["path"]) / "info.json").write_text(
        json.dumps({"pinned": True, "pin_order": 2}),
        encoding="utf-8",
    )
    (Path(gamma["path"]) / "info.json").write_text(
        json.dumps({"pinned": True, "pin_order": 1}),
        encoding="utf-8",
    )

    payload = {"profiles": list(rows), "active": "default"}
    with patch("integration.profiles.enrich._list_skills_for_profile", return_value=[]):
        out = enrich_profiles_response(payload)

    names = [p["name"] for p in out["profiles"]]
    assert names == ["gamma", "beta", "default", "alpha"]


def test_pin_limit_rejects_sixth(tmp_path):
    rows = _fake_profiles(tmp_path)
    named = [r for r in rows if r["name"] != "default"]
    extra = []
    for i in range(PROFILE_PIN_LIMIT - len(named) + 1):
        name = f"extra{i}"
        path = tmp_path / "profiles" / name
        path.mkdir(parents=True)
        extra.append(_profile_row(name, path))
    all_rows = rows + extra

    non_default = [r for r in all_rows if r["name"] != "default"]
    with _profile_patches(all_rows)[0], _profile_patches(all_rows)[1], _profile_patches(all_rows)[2]:
        for row in non_default[:PROFILE_PIN_LIMIT]:
            set_profile_pinned(row["name"], True)
        with pytest.raises(ValueError, match="Up to 5 profiles can be pinned"):
            set_profile_pinned(non_default[PROFILE_PIN_LIMIT]["name"], True)


def test_new_pin_goes_to_top(tmp_path):
    rows = _fake_profiles(tmp_path)
    alpha = next(r for r in rows if r["name"] == "alpha")
    beta = next(r for r in rows if r["name"] == "beta")

    with _profile_patches(rows)[0], _profile_patches(rows)[1], _profile_patches(rows)[2]:
        set_profile_pinned("alpha", True)
        set_profile_pinned("beta", True)

    alpha_info = json.loads((Path(alpha["path"]) / "info.json").read_text(encoding="utf-8"))
    beta_info = json.loads((Path(beta["path"]) / "info.json").read_text(encoding="utf-8"))
    assert beta_info["pin_order"] == 1
    assert alpha_info["pin_order"] == 2

    payload = {"profiles": list(rows), "active": "default"}
    with patch("integration.profiles.enrich._list_skills_for_profile", return_value=[]):
        out = enrich_profiles_response(payload)
    names = [p["name"] for p in out["profiles"]]
    assert names[:2] == ["beta", "alpha"]


def test_pin_default_allowed(tmp_path):
    rows = _fake_profiles(tmp_path)
    default = next(r for r in rows if r["name"] == "default")
    with _profile_patches(rows)[0], _profile_patches(rows)[1], _profile_patches(rows)[2]:
        out = set_profile_pinned("default", True)

    info_path = Path(default["path"]) / "info.json"
    data = json.loads(info_path.read_text(encoding="utf-8"))
    assert data["pinned"] is True
    assert data["pin_order"] == 1
    assert out["profile"]["info"]["pinned"] is True


def test_pin_missing_profile_404(tmp_path):
    rows = _fake_profiles(tmp_path)
    with _profile_patches(rows)[0]:
        with pytest.raises(FileNotFoundError):
            set_profile_pinned("missing", True)


def test_enrich_exposes_pin_fields_in_info(tmp_path):
    rows = _fake_profiles(tmp_path)
    alpha = next(r for r in rows if r["name"] == "alpha")
    (Path(alpha["path"]) / "info.json").write_text(
        json.dumps({"display_name": "Alpha", "pinned": True, "pin_order": 1}),
        encoding="utf-8",
    )
    payload = {"profiles": list(rows), "active": "default"}
    with patch("integration.profiles.enrich._list_skills_for_profile", return_value=[]):
        out = enrich_profiles_response(payload)

    entry = next(p for p in out["profiles"] if p["name"] == "alpha")
    assert entry["info"]["pinned"] is True
    assert entry["info"]["pin_order"] == 1
    assert entry["info"]["display_name"] == "Alpha"
    assert "pinned" not in entry
    assert "pin_order" not in entry
