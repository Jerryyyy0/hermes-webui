"""Keep Agent-owned provenance across the WebUI history boundary."""

from typing import Any

from integration.agent_message_semantics.classifier import control_message_semantics


def carry_agent_semantics(source: dict[str, Any], projected: dict[str, Any]) -> None:
    """Copy only recognized semantic fields, never arbitrary private metadata.

    Called while each source row is projected, before filtering can change row
    positions. Provider-bound projections must not call this helper: Agent's
    transport strips these fields from its request copy, not its durable history.
    """
    semantics = control_message_semantics(source)
    if semantics is not None:
        projected['_hermes_message_class'], projected['_hermes_scaffold_kind'] = semantics
