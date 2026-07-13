from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


def test_profiles_route_returns_active_profile(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    expected_profiles = [{"name": "default", "is_default": True}]

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: expected_profiles)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )

    response = routes.handle_get(SimpleNamespace(), urlparse("/api/profiles"))

    assert response == {
        "status": 200,
        "payload": {
            "profiles": expected_profiles,
            "active": "default",
        },
    }


def test_skills_route_resolves_explicit_profile(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.routes as routes

    profile_home = tmp_path / "profiles" / "research"
    captured = {}

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: profile_home)
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("/active/skills"))

    def fake_list(skills_dir, category=None, config_path=None):
        captured.update(
            skills_dir=skills_dir,
            category=category,
            config_path=config_path,
        )
        return {"skills": [{"name": "research-skill"}]}

    monkeypatch.setattr(routes, "_skills_list_from_dir", fake_list)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )

    response = routes.handle_get(
        SimpleNamespace(),
        urlparse("/api/skills?profile=research&category=tools"),
    )

    assert captured == {
        "skills_dir": profile_home / "skills",
        "category": "tools",
        "config_path": profile_home / "config.yaml",
    }
    assert response["payload"] == {"skills": [{"name": "research-skill"}]}
