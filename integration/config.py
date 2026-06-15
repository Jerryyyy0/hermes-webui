"""Shared integration configuration (env flags, SkillHub URL)."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE = _REPO_ROOT / "VERSION.txt"


def print_version_txt() -> None:
    """Print repo-root VERSION.txt at server startup (build/update stamp)."""
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError:
        return
    if text:
        print(text, flush=True)
        print("", flush=True)


def integration_enabled() -> bool:
    raw = os.getenv("HERMES_INTEGRATION", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def skillhub_url() -> str | None:
    raw = os.getenv("SKILLHUB_URL", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def skillhub_enabled() -> bool:
    return integration_enabled() and bool(skillhub_url())


def cron_all_profiles_enabled() -> bool:
    return integration_enabled()


def egress_policy_enabled() -> bool:
    raw = os.getenv("HERMES_EGRESS_POLICY_ENABLED", "").strip().lower()
    return integration_enabled() and raw in ("1", "true", "yes", "on")


def egress_policy_rules_path() -> str:
    return str(os.getenv("HERMES_EGRESS_POLICY_RULES_PATH", "").strip() or "/etc/iptables/rules.v4")


def zhiling_control_plane_url() -> str | None:
    raw = os.getenv("ZHILING_CONTROL_PLANE_URL", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def identity_lookup_enabled() -> bool:
    return integration_enabled() and bool(zhiling_control_plane_url())


def zhiling_logout_base_url() -> str | None:
    """auth-proxy origin only (e.g. http://auth-proxy:8080); path is fixed in code."""
    raw = os.getenv("ZHILING_LOGOUT_API_URL", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def zhiling_logout_enabled() -> bool:
    return integration_enabled() and bool(zhiling_logout_base_url())


def knowledge_base_url() -> str | None:
    raw = os.getenv("KNOWLEDGE_BASE_URL", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def knowledge_base_enabled() -> bool:
    return integration_enabled() and bool(knowledge_base_url())


def egress_capture_dir() -> str:
    return str(os.getenv("HERMES_EGRESS_CAPTURE_DIR", "").strip() or "/var/log/egress")


def _egress_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def egress_nflog_group_allowed() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", 100)


def egress_nflog_group_denied() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_DENIED", 200)