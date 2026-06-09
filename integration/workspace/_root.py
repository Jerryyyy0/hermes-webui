"""Resolve the integration workspace root (HERMES_WEBUI_DEFAULT_WORKSPACE)."""

from __future__ import annotations

from pathlib import Path

from api.workspace import resolve_trusted_workspace, safe_resolve_ws


def integration_workspace_root() -> Path:
    return resolve_trusted_workspace(None)


def resolve_integration_rel(rel: str) -> Path:
    return safe_resolve_ws(integration_workspace_root(), rel)
