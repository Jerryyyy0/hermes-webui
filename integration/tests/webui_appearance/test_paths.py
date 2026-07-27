"""Tests for webui_appearance path resolution."""

from __future__ import annotations

import api.profiles as profiles
from integration.webui_appearance.paths import config_path, hermes_home


def test_hermes_home_uses_base_home_not_runtime_profile_env(tmp_path, monkeypatch):
    base = tmp_path / "base"
    profile_home = base / "profiles" / "abc"
    profile_home.mkdir(parents=True)
    appearance_dir = base / "webui-appearance"
    appearance_dir.mkdir()
    config_file = appearance_dir / "webui-appearance.json"
    config_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert hermes_home() == base.resolve()
    assert config_path() == config_file.resolve()
    assert config_path().is_file()
