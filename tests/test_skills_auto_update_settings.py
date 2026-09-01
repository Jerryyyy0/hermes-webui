def test_skills_auto_update_defaults_off_and_persists(tmp_path, monkeypatch):
    import api.config as config

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")

    assert config.load_settings()["skills_auto_update"] is False
    assert config.save_settings({"skills_auto_update": True})["skills_auto_update"] is True
    assert config.load_settings()["skills_auto_update"] is True
    assert config.save_settings({"skills_auto_update": False})["skills_auto_update"] is False
