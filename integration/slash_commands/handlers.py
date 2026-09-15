"""HTTP handler for the external slash-command catalog."""

from __future__ import annotations

from api.helpers import j
from integration.config import integration_enabled


_PATH = "/api/integration/slash_commands"


def _catalog_payload() -> dict:
    """Return a fresh copy of the public V1 command catalog."""
    return {
        "commands": [
            {
                "name": "compact",
                "description": "压缩当前会话上下文，减少后续模型调用携带的历史内容。",
                "args_hint": "[focus topic]",
            }
        ],
    }


def try_handle_get(handler, parsed) -> bool:
    """Handle the catalog endpoint when the integration layer is enabled."""
    if not integration_enabled() or parsed.path != _PATH:
        return False

    j(handler, _catalog_payload())
    return True
