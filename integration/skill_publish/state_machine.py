"""State transition checks for skill applications (docs §5)."""

from __future__ import annotations

from integration.skill_publish.constants import AuditAction, PublishStatus

# (current_status, action) -> target_status
_TRANSITIONS: dict[tuple[str, str], str] = {
    (PublishStatus.DRAFT, AuditAction.SUBMIT): PublishStatus.PENDING,
    (PublishStatus.REJECTED, AuditAction.SUBMIT): PublishStatus.PENDING,
    (PublishStatus.PENDING, AuditAction.WITHDRAW): PublishStatus.DRAFT,
    (PublishStatus.PENDING, AuditAction.APPROVE): PublishStatus.APPROVED,
    (PublishStatus.PENDING, AuditAction.REJECT): PublishStatus.REJECTED,
}


def can_transition(current: str, action: str) -> bool:
    return (current, action) in _TRANSITIONS


def target_status(current: str, action: str) -> str | None:
    return _TRANSITIONS.get((current, action))
