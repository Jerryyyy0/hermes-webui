"""Shared integration configuration (env flags, SkillHub URL)."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE = _REPO_ROOT / "VERSION.txt"


def print_version_txt() -> None:
    """Log repo-root VERSION.txt at server startup (build/update stamp)."""
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError:
        return
    if text:
        try:
            from integration.project_logging import log_info

            log_info(text)
        except Exception:
            pass


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


def skill_publish_enabled() -> bool:
    """Skill publish application flow master switch (docs/integration/skill-publish-flow设计方案.md)."""
    if not integration_enabled() or not skillhub_url():
        return False
    raw = os.getenv("SKILL_PUBLISH_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def skill_publish_platform() -> str:
    """Third-party platform marker sent to upstream SkillHub (B2 upload)."""
    return os.getenv("SKILL_PUBLISH_PLATFORM", "").strip() or "hermes-webui"


def skill_publish_user_account() -> str:
    """Current project user account (one project = one user)."""
    return os.getenv("SKILL_PUBLISH_USER_ACCOUNT", "").strip() or os.getenv(
        "USER", "user"
    )


def skill_publish_user_uuid() -> str:
    """Current project user UUID (one project = one user).

    Used as externalUserId for upstream SkillHub API calls.
    """
    return os.getenv("SKILL_PUBLISH_USER_UUID", "").strip() or os.getenv(
        "SKILL_PUBLISH_USER_ACCOUNT", ""
    )


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


def zhiling_identity_cache_ttl_seconds() -> int:
    """Default TTL for in-process Zhiling identity cache when JWT exp is absent."""
    return _egress_int_env("ZHILING_IDENTITY_CACHE_TTL_SECONDS", 1800)


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


def webui_backend_mode() -> str:
    """BACKEND env: ``local`` skips Zhiling downstream calls; empty/remote use them."""
    return os.getenv("BACKEND", "").strip().lower()


def webui_backend_is_local() -> bool:
    return webui_backend_mode() == "local"


def knowledge_base_url() -> str | None:
    raw = os.getenv("KNOWLEDGE_BASE_URL", "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        return None
    return raw.rstrip("/")


def knowledge_base_enabled() -> bool:
    return integration_enabled() and bool(knowledge_base_url())


# 出口流量抓包目录，tcpdump 在此写轮转 pcap，读取接口从这里取文件。
def egress_capture_dir() -> str:
    return str(os.getenv("HERMES_EGRESS_CAPTURE_DIR", "").strip() or "/var/log/egress")


# 从环境变量读取整数，缺省或非法值时回落到默认值。
def _egress_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 放行流量记录使用的 NFLOG group 号（需与容器内 tcpdump -i nflog:N 保持一致）。
def egress_nflog_group_allowed() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", 100)


# 被拒绝流量记录使用的 NFLOG group 号。
def egress_nflog_group_denied() -> int:
    return _egress_int_env("HERMES_EGRESS_NFLOG_GROUP_DENIED", 200)