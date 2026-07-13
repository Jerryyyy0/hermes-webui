"""Profile gateway startup integration."""

from integration.gateway_startup.startup import (
    ensure_all_profile_gateways,
    gateway_autostart_enabled,
)

__all__ = ["ensure_all_profile_gateways", "gateway_autostart_enabled"]
