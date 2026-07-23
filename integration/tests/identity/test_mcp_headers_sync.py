"""Tests for ithink_kb_mcp header sync on webui_login."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import yaml

from integration.identity.handlers import try_handle_get
from integration.identity.mcp_headers_sync import (
    ACCOUNT_KEY,
    SERVER_NAME,
    UUID_KEY,
    extract_ithink_account_uuid,
    sync_ithink_kb_mcp_headers,
)
from integration.identity.session_store import clear_session


def setup_function():
    clear_session()


def test_extract_uses_only_ithinktank_account_and_uuid():
    identity = {
        "ithinktank": {"account": "nested-acc", "uuid": "nested-uuid", "userId": "nested-uid"},
        "ithinktank_account": "flat-acc",
        "ithinktank_user_id": "flat-uid",
    }
    assert extract_ithink_account_uuid(identity) == ("nested-acc", "nested-uuid")


def test_extract_ignores_userId_and_flat_fallbacks():
    # userId alone is not enough — only ithinktank.uuid is used.
    assert (
        extract_ithink_account_uuid(
            {
                "ithinktank": {"account": "acc", "userId": "from-user-id"},
                "ithinktank_account": "flat-acc",
            }
        )
        is None
    )
    assert (
        extract_ithink_account_uuid(
            {
                "ithinktank_account": "only-flat",
                "ithinktank_user_id": "only-flat-uid",
            }
        )
        is None
    )


def test_extract_returns_none_when_missing():
    assert extract_ithink_account_uuid({}) is None
    assert extract_ithink_account_uuid({"ithinktank": {"account": "a"}}) is None
    assert extract_ithink_account_uuid({"ithinktank": {"uuid": "u"}}) is None
    assert extract_ithink_account_uuid({"ithinktank_user_id": "u"}) is None


def _write_config(home: Path, data: dict) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def test_sync_updates_only_ithink_kb_mcp(tmp_path: Path):
    with_mcp = tmp_path / "with_mcp"
    without_mcp = tmp_path / "without_mcp"
    other_server = tmp_path / "other_server"
    no_config = tmp_path / "no_config"
    no_config.mkdir()

    _write_config(
        with_mcp,
        {
            "mcp_servers": {
                SERVER_NAME: {
                    "url": "http://example/mcp",
                    "headers": {
                        ACCOUNT_KEY: "old",
                        UUID_KEY: "old-uuid",
                        "X-IThink-IsPersonal": "1",
                    },
                    "timeout": 120,
                }
            }
        },
    )
    _write_config(without_mcp, {"model": {"default": "x"}})
    _write_config(
        other_server,
        {
            "mcp_servers": {
                "other": {
                    "url": "http://other",
                    "headers": {ACCOUNT_KEY: "keep", UUID_KEY: "keep"},
                }
            }
        },
    )

    profiles = [
        {"name": "with_mcp", "path": str(with_mcp)},
        {"name": "without_mcp", "path": str(without_mcp)},
        {"name": "other_server", "path": str(other_server)},
        {"name": "no_config", "path": str(no_config)},
    ]
    identity = {"ithinktank": {"account": "gaoxiang", "uuid": "uuid-1"}}

    with patch("api.profiles.list_profiles_api", return_value=profiles):
        with patch("api.profiles.get_active_hermes_home", return_value=with_mcp):
            with patch("integration.identity.mcp_headers_sync.reload_config") as reload_mock:
                stats = sync_ithink_kb_mcp_headers(identity)

    assert stats["updated"] == 1
    assert stats["skipped"] == 3
    assert stats["errors"] == 0
    reload_mock.assert_called_once()

    updated = yaml.safe_load((with_mcp / "config.yaml").read_text(encoding="utf-8"))
    headers = updated["mcp_servers"][SERVER_NAME]["headers"]
    assert headers[ACCOUNT_KEY] == "gaoxiang"
    assert headers[UUID_KEY] == "uuid-1"
    assert headers["X-IThink-IsPersonal"] == "1"

    other = yaml.safe_load((other_server / "config.yaml").read_text(encoding="utf-8"))
    assert other["mcp_servers"]["other"]["headers"][ACCOUNT_KEY] == "keep"


def test_sync_idempotent_skips_write(tmp_path: Path):
    home = tmp_path / "p1"
    _write_config(
        home,
        {
            "mcp_servers": {
                SERVER_NAME: {
                    "headers": {ACCOUNT_KEY: "same", UUID_KEY: "same-uuid"},
                }
            }
        },
    )
    identity = {"ithinktank": {"account": "same", "uuid": "same-uuid"}}
    with patch("api.profiles.list_profiles_api", return_value=[{"name": "p1", "path": str(home)}]):
        with patch("api.profiles.get_active_hermes_home", return_value=tmp_path / "other"):
            with patch("integration.identity.mcp_headers_sync._save_yaml_config_file") as save_mock:
                stats = sync_ithink_kb_mcp_headers(identity)
    assert stats == {"updated": 0, "skipped": 1, "errors": 0}
    save_mock.assert_not_called()


def test_sync_fills_missing_headers_on_matched_server(tmp_path: Path):
    """Matched by name: create headers / missing keys instead of skipping."""
    no_headers = tmp_path / "no_headers"
    partial = tmp_path / "partial"
    _write_config(
        no_headers,
        {
            "mcp_servers": {
                SERVER_NAME: {"url": "http://example/mcp", "timeout": 120},
                "other": {"url": "http://other", "headers": {"Authorization": "x"}},
            }
        },
    )
    _write_config(
        partial,
        {
            "mcp_servers": {
                SERVER_NAME: {
                    "headers": {
                        ACCOUNT_KEY: "stale",
                        "X-IThink-IsPersonal": "1",
                    }
                }
            }
        },
    )
    identity = {"ithinktank": {"account": "gaoxiang", "uuid": "uuid-1"}}
    profiles = [
        {"name": "no_headers", "path": str(no_headers)},
        {"name": "partial", "path": str(partial)},
    ]
    with patch("api.profiles.list_profiles_api", return_value=profiles):
        with patch("api.profiles.get_active_hermes_home", return_value=tmp_path / "none"):
            stats = sync_ithink_kb_mcp_headers(identity)

    assert stats["updated"] == 2
    assert stats["errors"] == 0

    filled = yaml.safe_load((no_headers / "config.yaml").read_text(encoding="utf-8"))
    assert filled["mcp_servers"][SERVER_NAME]["headers"] == {
        ACCOUNT_KEY: "gaoxiang",
        UUID_KEY: "uuid-1",
    }
    assert filled["mcp_servers"]["other"]["headers"] == {"Authorization": "x"}

    partial_cfg = yaml.safe_load((partial / "config.yaml").read_text(encoding="utf-8"))
    assert partial_cfg["mcp_servers"][SERVER_NAME]["headers"] == {
        ACCOUNT_KEY: "gaoxiang",
        "X-IThink-IsPersonal": "1",
        UUID_KEY: "uuid-1",
    }


def test_sync_skips_all_when_identity_incomplete():
    with patch("api.profiles.list_profiles_api") as list_mock:
        stats = sync_ithink_kb_mcp_headers({"username": "x"})
    assert stats == {"updated": 0, "skipped": 0, "errors": 0}
    list_mock.assert_not_called()


def test_sync_skip_logs_extract_diagnostics(capsys):
    stats = sync_ithink_kb_mcp_headers(
        {
            "username": "liuweijun",
            "ithinktank": {"account": "liuweijun", "userId": "only-user-id"},
        }
    )
    assert stats["updated"] == 0
    err = capsys.readouterr().err
    assert "skip missing ithink account/uuid" in err
    assert "reason=missing_uuid" in err
    assert "username=liuweijun" in err
    assert "has_ithinktank_account=True" in err
    assert "has_ithinktank_uuid=False" in err
    assert "ithinktank_type=dict" in err


def test_sync_isolates_errors_and_logs_success_failure(tmp_path: Path, capsys):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    _write_config(
        good,
        {"mcp_servers": {SERVER_NAME: {"headers": {ACCOUNT_KEY: "old", UUID_KEY: "old"}}}},
    )
    _write_config(
        bad,
        {"mcp_servers": {SERVER_NAME: {"headers": {ACCOUNT_KEY: "old", UUID_KEY: "old"}}}},
    )
    identity = {"ithinktank": {"account": "acc", "uuid": "uid"}}
    profiles = [
        {"name": "bad", "path": str(bad)},
        {"name": "good", "path": str(good)},
    ]

    from api.config import _save_yaml_config_file as _real_save

    def _save(path, cfg):
        if Path(path).parent.name == "bad":
            raise OSError("disk full")
        return _real_save(path, cfg)

    with patch("api.profiles.list_profiles_api", return_value=profiles):
        with patch("api.profiles.get_active_hermes_home", return_value=tmp_path / "none"):
            with patch("integration.identity.mcp_headers_sync._save_yaml_config_file", side_effect=_save):
                stats = sync_ithink_kb_mcp_headers(identity)

    assert stats["errors"] == 1
    assert stats["updated"] == 1
    good_cfg = yaml.safe_load((good / "config.yaml").read_text(encoding="utf-8"))
    assert good_cfg["mcp_servers"][SERVER_NAME]["headers"][ACCOUNT_KEY] == "acc"

    err = capsys.readouterr().err
    assert "profile=good updated ok" in err
    assert "profile=bad update failed" in err
    assert "[webui][integration_login][mcp_headers] done" in err


def test_handler_bearer_200_calls_sync():
    identity = {"username": "zhangsan", "ithinktank": {"account": "a", "uuid": "u"}}
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with patch(
                "integration.identity.mcp_headers_sync.sync_ithink_kb_mcp_headers",
                return_value={"updated": 1, "skipped": 0, "errors": 0},
            ) as sync_mock:
                assert try_handle_get(handler, parsed) is True
    sync_mock.assert_called_once_with(identity)
    handler.send_response.assert_called_with(200)


def test_handler_no_bearer_does_not_call_sync():
    from integration.identity.session_store import save_session

    save_session("tok", {"username": "cached"})
    handler = MagicMock()
    handler.headers = {}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.mcp_headers_sync.sync_ithink_kb_mcp_headers"
        ) as sync_mock:
            assert try_handle_get(handler, parsed) is True
    sync_mock.assert_not_called()
    handler.send_response.assert_called_with(200)


def test_handler_non_200_does_not_call_sync():
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer bad"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(401, {"detail": "no"}),
        ):
            with patch(
                "integration.identity.mcp_headers_sync.sync_ithink_kb_mcp_headers"
            ) as sync_mock:
                assert try_handle_get(handler, parsed) is True
    sync_mock.assert_not_called()
    handler.send_response.assert_called_with(401)


def test_handler_sync_exception_still_returns_200():
    identity = {"username": "zhangsan", "ithinktank": {"account": "a", "uuid": "u"}}
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer secret-token"}
    parsed = urlparse("/api/integration/webui_login")
    with patch("integration.identity.handlers.identity_lookup_enabled", return_value=True):
        with patch(
            "integration.identity.handlers.lookup_current_identity",
            return_value=(200, identity),
        ):
            with patch(
                "integration.identity.mcp_headers_sync.sync_ithink_kb_mcp_headers",
                side_effect=RuntimeError("boom"),
            ):
                assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    payload = json.loads(raw)
    assert payload["username"] == "zhangsan"
