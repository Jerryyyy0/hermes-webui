"""Shared integration configuration (env flags, SkillHub URL)."""

from __future__ import annotations

import os


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