"""Upstream seam: integration must not intercept local /api/skills routes."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.skills.handlers import try_handle_get, try_handle_post


def test_handlers_noop_when_disabled():
    parsed = urlparse("/api/skills")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False
        assert try_handle_post(handler, parsed, {}) is False


def test_local_skills_not_intercepted_when_skillhub_enabled():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        assert try_handle_get(handler, urlparse("/api/skills")) is False
        assert try_handle_get(handler, urlparse("/api/skills/content?name=x")) is False
        assert try_handle_post(handler, urlparse("/api/skills/save"), {}) is False


def test_skillhub_routes_handled_when_enabled():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.listing.list_skillhub_skills") as lst:
            with patch("integration.skills.handlers.j", return_value=True):
                lst.return_value = {"skills": [], "total": 0, "stats": {}}
                assert try_handle_get(handler, urlparse("/api/skillhub/skills?category=tools")) is True
