"""Sync ithink_kb_mcp identity headers across profile config.yaml files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.config import _load_yaml_config_file, _save_yaml_config_file, reload_config
from integration.project_logging import console_info, console_warning, format_kv, one_line, with_timestamp

SERVER_NAME = "ithink_kb_mcp"
ACCOUNT_KEY = "X-IThink-Account"
UUID_KEY = "X-IThink-UUID"


def _as_nonempty_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _identity_keys(identity: Any, *, limit: int = 24) -> list[str]:
    if not isinstance(identity, dict):
        return []
    keys = sorted(str(k) for k in identity.keys())
    if len(keys) > limit:
        return keys[:limit] + [f"...(+{len(keys) - limit})"]
    return keys


def diagnose_ithink_extract(identity: Any) -> dict[str, Any]:
    """Return non-secret diagnostics explaining extract_ithink_account_uuid failure/success."""
    info: dict[str, Any] = {
        "identity_type": type(identity).__name__,
        "keys": ",".join(_identity_keys(identity)) or "-",
    }
    if not isinstance(identity, dict):
        info["reason"] = "identity_not_dict"
        return info

    info["username"] = _as_nonempty_str(identity.get("username")) or "-"

    ithink = identity.get("ithinktank")
    info["ithinktank_type"] = type(ithink).__name__ if ithink is not None else "None"
    if isinstance(ithink, dict):
        info["ithinktank_keys"] = ",".join(_identity_keys(ithink)) or "-"
        account = _as_nonempty_str(ithink.get("account"))
        uuid = _as_nonempty_str(ithink.get("uuid"))
        info["has_ithinktank_account"] = bool(account)
        info["has_ithinktank_uuid"] = bool(uuid)
    else:
        account = ""
        uuid = ""
        info["has_ithinktank_account"] = False
        info["has_ithinktank_uuid"] = False

    if account and uuid:
        info["reason"] = "ok"
    elif not account and not uuid:
        info["reason"] = "missing_both_account_and_uuid"
    elif not account:
        info["reason"] = "missing_account"
    else:
        info["reason"] = "missing_uuid"
    info["resolved_account"] = bool(account)
    info["resolved_uuid"] = bool(uuid)
    return info


def extract_ithink_account_uuid(identity: dict) -> tuple[str, str] | None:
    """Return (account, uuid) from identity.ithinktank only — no fallbacks."""
    if not isinstance(identity, dict):
        return None

    ithink = identity.get("ithinktank")
    if not isinstance(ithink, dict):
        return None

    account = _as_nonempty_str(ithink.get("account"))
    uuid = _as_nonempty_str(ithink.get("uuid"))
    if not account or not uuid:
        return None
    return account, uuid

def _profile_home(row: dict) -> Path | None:
    raw = row.get("path")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def _patch_server_headers(server: dict, account: str, uuid: str) -> bool:
    """Set X-IThink-Account / X-IThink-UUID on a matched server.

    Creates ``headers`` when missing and fills either key when absent.
    Return True if a write is needed.
    """
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
    """Update X-IThink-* headers on ``mcp_servers.ithink_kb_mcp`` for every profile.

    Matched by server name only. When the server exists, always ensure both
    header keys are present (create ``headers`` / missing keys as needed).
    Other MCP servers are left untouched.

    Returns stats: ``{"updated": int, "skipped": int, "errors": int}``.
    Missing account/uuid skips the whole sync (no profile writes).
    Per-profile failures are isolated and do not raise.
    """
    stats = {"updated": 0, "skipped": 0, "errors": 0}
    extracted = extract_ithink_account_uuid(identity)
    if extracted is None:
        diag = diagnose_ithink_extract(identity)
        reason = diag.get("reason") or "unknown"
        # Put reason / key presence first so truncation still leaves the diagnosis.
        head = {
            "reason": reason,
            "username": diag.get("username"),
            "ithinktank_type": diag.get("ithinktank_type"),
            "has_ithinktank_account": diag.get("has_ithinktank_account"),
            "has_ithinktank_uuid": diag.get("has_ithinktank_uuid"),
            "keys": diag.get("keys"),
        }
        if diag.get("ithinktank_keys"):
            head["ithinktank_keys"] = diag.get("ithinktank_keys")
        extras = format_kv(head)
        console_warning(
            with_timestamp(
                "[webui][integration_login][mcp_headers] "
                + one_line(f"skip missing ithink account/uuid {extras}", max_len=0),
                {"event": "mcp_headers_sync", "phase": "skip", **diag},
            )
        )
        return stats
    account, uuid = extracted
    console_info(
        with_timestamp(
            "[webui][integration_login][mcp_headers] "
            + one_line(f"start server={SERVER_NAME} account={account}"),
            {
                "event": "mcp_headers_sync",
                "phase": "start",
                "server": SERVER_NAME,
                "account": account,
            },
        )
    )

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

    console_info(
        with_timestamp(
            "[webui][integration_login][mcp_headers] "
            + one_line(f"profiles={len(profiles)}"),
            {
                "event": "mcp_headers_sync",
                "phase": "list_ok",
                "profiles": len(profiles),
            },
        )
    )

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
                console_info(
                    with_timestamp(
                        "[webui][integration_login][mcp_headers] "
                        + one_line(f"profile={profile_name} skip no config.yaml path={config_path}"),
                        {
                            "event": "mcp_headers_sync",
                            "phase": "skipped",
                            "profile": profile_name,
                            "reason": "no_config",
                            "path": str(config_path),
                        },
                    )
                )
                continue
            cfg = _load_yaml_config_file(config_path)
            if not isinstance(cfg, dict):
                cfg = {}
            servers = cfg.get("mcp_servers")
            if not isinstance(servers, dict):
                stats["skipped"] += 1
                console_info(
                    with_timestamp(
                        "[webui][integration_login][mcp_headers] "
                        + one_line(f"profile={profile_name} skip no mcp_servers path={config_path}"),
                        {
                            "event": "mcp_headers_sync",
                            "phase": "skipped",
                            "profile": profile_name,
                            "reason": "no_mcp_servers",
                            "path": str(config_path),
                        },
                    )
                )
                continue
            server = servers.get(SERVER_NAME)
            if not isinstance(server, dict):
                stats["skipped"] += 1
                console_info(
                    with_timestamp(
                        "[webui][integration_login][mcp_headers] "
                        + one_line(
                            f"profile={profile_name} skip no server={SERVER_NAME} "
                            f"path={config_path}"
                        ),
                        {
                            "event": "mcp_headers_sync",
                            "phase": "skipped",
                            "profile": profile_name,
                            "reason": "no_server",
                            "server": SERVER_NAME,
                            "path": str(config_path),
                        },
                    )
                )
                continue
            # Mutate a copy so we do not share nested refs with the load cache.
            server = dict(server)
            if not _patch_server_headers(server, account, uuid):
                stats["skipped"] += 1
                console_info(
                    with_timestamp(
                        "[webui][integration_login][mcp_headers] "
                        + one_line(f"profile={profile_name} skip unchanged path={config_path}"),
                        {
                            "event": "mcp_headers_sync",
                            "phase": "skipped",
                            "profile": profile_name,
                            "reason": "unchanged",
                            "path": str(config_path),
                        },
                    )
                )
                continue
            servers = dict(servers)
            servers[SERVER_NAME] = server
            cfg = dict(cfg)
            cfg["mcp_servers"] = servers
            _save_yaml_config_file(config_path, cfg)
            stats["updated"] += 1
            console_info(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line(
                        f"profile={profile_name} updated ok path={config_path} "
                        f"account={account}"
                    ),
                    {
                        "event": "mcp_headers_sync",
                        "phase": "updated",
                        "profile": profile_name,
                        "server": SERVER_NAME,
                        "path": str(config_path),
                        "account": account,
                    },
                )
            )
            if active_home is not None and home.resolve() == active_home:
                touched_active = True
        except Exception as exc:
            stats["errors"] += 1
            console_warning(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line(
                        f"profile={profile_name} update failed path={config_path}: {exc}"
                    ),
                    {
                        "event": "mcp_headers_sync",
                        "phase": "update_failed",
                        "profile": profile_name,
                        "server": SERVER_NAME,
                        "path": str(config_path),
                        "error": str(exc),
                    },
                )
            )

    if touched_active:
        try:
            reload_config()
            console_info(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line("reload_config ok"),
                    {"event": "mcp_headers_sync", "phase": "reload_ok"},
                )
            )
        except Exception as exc:
            console_warning(
                with_timestamp(
                    "[webui][integration_login][mcp_headers] "
                    + one_line(f"reload_config failed: {exc}"),
                    {
                        "event": "mcp_headers_sync",
                        "phase": "reload_failed",
                        "error": str(exc),
                    },
                )
            )

    extras = format_kv(stats)
    done_line = with_timestamp(
        f"[webui][integration_login][mcp_headers] done {extras}".rstrip(),
        {"event": "mcp_headers_sync", "phase": "done", **stats},
    )
    if stats["errors"]:
        console_warning(done_line)
    else:
        console_info(done_line)
    return stats
