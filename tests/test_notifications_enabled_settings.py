def test_notification_settings_default_off_and_persist(tmp_path, monkeypatch):
    import api.config as config

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")

    assert config.load_settings()["notifications_enabled"] is False
    assert config.save_settings({"notifications_enabled": True})["notifications_enabled"] is True
    assert config.load_settings()["notifications_enabled"] is True
    assert config.save_settings({"notifications_enabled": False})["notifications_enabled"] is False

    assert config.load_settings()["sound_enabled"] is False
    assert config.save_settings({"sound_enabled": True})["sound_enabled"] is True
    assert config.load_settings()["sound_enabled"] is True
    assert config.save_settings({"sound_enabled": False})["sound_enabled"] is False
