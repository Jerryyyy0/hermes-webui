import os
from unittest.mock import patch

from integration.config import (
    webui_backend_is_local,
    webui_backend_mode,
    zhiling_logout_base_url,
    zhiling_logout_enabled,
)


def test_logout_disabled_without_url():
    with patch.dict(os.environ, {"HERMES_INTEGRATION": "1"}, clear=False):
        os.environ.pop("ZHILING_LOGOUT_API_URL", None)
        assert zhiling_logout_base_url() is None
        assert zhiling_logout_enabled() is False


def test_logout_enabled_with_valid_base_url():
    env = {
        "HERMES_INTEGRATION": "1",
        "ZHILING_LOGOUT_API_URL": "http://auth-proxy:8080/",
    }
    with patch.dict(os.environ, env, clear=False):
        assert zhiling_logout_base_url() == "http://auth-proxy:8080"
        assert zhiling_logout_enabled() is True


def test_logout_invalid_url_scheme():
    env = {
        "HERMES_INTEGRATION": "1",
        "ZHILING_LOGOUT_API_URL": "ftp://auth-proxy:8080",
    }
    with patch.dict(os.environ, env, clear=False):
        assert zhiling_logout_base_url() is None
        assert zhiling_logout_enabled() is False


def test_webui_backend_mode_defaults_to_empty():
    with patch.dict(os.environ, {}, clear=True):
        assert webui_backend_mode() == ""
        assert webui_backend_is_local() is False


def test_webui_backend_mode_local():
    with patch.dict(os.environ, {"BACKEND": "local"}, clear=False):
        assert webui_backend_mode() == "local"
        assert webui_backend_is_local() is True


def test_webui_backend_mode_remote():
    with patch.dict(os.environ, {"BACKEND": "remote"}, clear=False):
        assert webui_backend_mode() == "remote"
        assert webui_backend_is_local() is False
