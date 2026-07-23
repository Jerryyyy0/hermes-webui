"""Sync ithink_kb_mcp identity headers across profile config.yaml files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.config import _load_yaml_config_file, _save_yaml_config_file, reload_config

try:
    from integration.project_logging import console_info, console_warning, format_kv, one_line, with_timestamp
except ImportError:  # pragma: no cover - fallback while request_logging still exists on older trees
    from integration.request_logging.formatting import format_kv, one_line, with_timestamp
    from integration.request_logging.logger import console_info, console_warning

SERVER_NAME = "ithink_kb_mcp"
ACCOUNT_KEY = "X-IThink-Account"
UUID_KEY = "X-IThink-UUID"


def _as_nonempty_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def extract_ithink_account_uuid(identity: dict) -> tuple[str, str] | None:
    """Return (account, uuid) from identity payload, or None if either is missing."""
    if not isinstance(identity, dict):
        return None

    ithink = identity.get("ithinktank")
    if not isinstance(ithink, dict):
        ithink = {}

    account = _as_nonempty_str(ithink.get("account")) or _as_nonempty_str(
        identity.get("ithinktank_account")
    )
    uuid = (
        _as_nonempty_str(ithink.get("uuid"))
        or _as_nonempty_str(ithink.get("userId"))
        or _as_nonempty_str(identity.get("ithinktank_user_id"))
    )
    if not account or not uuid:
        return None
    return account, uuid


def _profile_home(row: dict) -> Path | None:
    raw = row.get("path")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def _patch_server_headers(server: dict, account: str, uuid: str) -> bool:
    """Mutate server headers in place. Return True if a write is needed."""
    headers = server.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    else:
        headers = dict(headers)
    if headers.get(ACCOUNT_KEY) == account and headers.get(UUID_KEY) == uuid:
        return False
    headers[ACCOUNT_KEY] = account
    headers[UUID_KEY] = uuid
    server["headers"] = headers
    return True


def sync_ithink_kb_mcp_headers(identity: dict) -> dict:
    """Update X-IThink-* headers on ithink_kb_mcp for every visible profile.

    Returns stats: ``{"updated": int, "skipped": int, "errors": int}``.
    Missing account/uuid skips the whole sync (no profile writes).
    Per-profile failures are isolated and do not raise.
    """
    stats = {"updated": 0, "skipped": 0, "errors": 0}
    extracted = extract_ithink_account_uuid(identity)
    if extracted is None:
        console_warning(
            with_timestamp(
                "[webui][integration_login][mcp_headers] "
                + one_line("skip missing ithink account/uuid"),
                {"event": "mcp_headers_sync", "phase": "skip"},
            )
        )
        return stats

    account, uuid = extracted

    from api.profiles import get_active_hermes_home, list_profiles_api

    try:
        profiles = list_profiles_api() or []
    except Exception as exc:
        console_warning(
            with_timestamp(
                "[webui][integration_login][mcp_headers] "
                + one_line(f"list_profiles failed: {exc}"),
                {"event": "mcp_headers_sync", "phase": "list_failed"},
            )
        )
        stats["errors"] += 1
        return stats

    try:
        active_home = get_active_hermes_home().resolve()
    except Exception:
        active_home = None

    touched_active = False

    for row in profiles:
        if not isinstance(row, dict):
            stats["skipped"] += 1
            continue
        home = _profile_home(row)
        if home is None:
            stats["skipped"] += 1
            continue
        config_path = home / "config.yaml"
        profile_name = str(row.get("name") or home.name)
        try:
            if not config_path.is_file():
                stats["skipped"] += 1
                continue
            cfg = _load_yaml_config_file(config_path)
            if not isinstance(cfg, dict):
                cfg = {}
            servers = cfg.get("mcp_servers")
            if not isinstance(servers, dict):
                stats["skipped"] += 1
                continue
            server = servers.get(SERVER_NAME)
            if not isinstance(server, dict):
                stats["skipped"] += 1
                continue
            # Mutate a copy so we do not share nested refs with the load cache.
            server = dict(server)
            if not _patch_server_headers(server, account, uuid):
                stats["skipped"] += 1
                continue
            servers = dict(servers)
            servers[SERVER_NAME] = server
            cfg = dict(cfg)
            cfg["mcp_servers"] = servers
            _save_yaml_config_file(config_path, cfg)
            stats["updated"] += 1
            if active_home is not None and home.resolve() == active_home:
                touched_active = True
        except Exception as exc:
            stats["errors"] += 1
            console_warning(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line(f"profile={profile_name} write failed: {exc}"),
                    {
                        "event": "mcp_headers_sync",
                        "phase": "write_failed",
                        "profile": profile_name,
                    },
                )
            )

    if touched_active:
        try:
            reload_config()
        except Exception as exc:
            console_warning(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line(f"reload_config failed: {exc}"),
                    {"event": "mcp_headers_sync", "phase": "reload_failed"},
                )
            )

    extras = format_kv(stats)
    console_info(
        with_timestamp(
            f"[webui][integration_login][mcp_headers] done {extras}".rstrip(),
            {"event": "mcp_headers_sync", "phase": "done", **stats},
        )
    )
    return stats
