"""Tests for skill_publish state machine."""

from integration.skill_publish.constants import AuditAction, PublishStatus
from integration.skill_publish.state_machine import can_transition, target_status


def test_submit_from_draft_or_rejected():
    assert can_transition(PublishStatus.DRAFT, AuditAction.SUBMIT)
    assert can_transition(PublishStatus.REJECTED, AuditAction.SUBMIT)
    assert target_status(PublishStatus.DRAFT, AuditAction.SUBMIT) == PublishStatus.PENDING
    assert target_status(PublishStatus.REJECTED, AuditAction.SUBMIT) == PublishStatus.PENDING


def test_submit_from_pending_or_approved_rejected():
    assert not can_transition(PublishStatus.PENDING, AuditAction.SUBMIT)
    assert not can_transition(PublishStatus.APPROVED, AuditAction.SUBMIT)


def test_withdraw_only_from_pending():
    assert can_transition(PublishStatus.PENDING, AuditAction.WITHDRAW)
    assert target_status(PublishStatus.PENDING, AuditAction.WITHDRAW) == PublishStatus.DRAFT
    for status in (PublishStatus.DRAFT, PublishStatus.APPROVED, PublishStatus.REJECTED):
        assert not can_transition(status, AuditAction.WITHDRAW)


def test_audit_results_only_from_pending():
    assert can_transition(PublishStatus.PENDING, AuditAction.APPROVE)
    assert target_status(PublishStatus.PENDING, AuditAction.APPROVE) == PublishStatus.APPROVED
    assert can_transition(PublishStatus.PENDING, AuditAction.REJECT)
    assert target_status(PublishStatus.PENDING, AuditAction.REJECT) == PublishStatus.REJECTED
    for status in (PublishStatus.DRAFT, PublishStatus.APPROVED, PublishStatus.REJECTED):
        assert not can_transition(status, AuditAction.APPROVE)
        assert not can_transition(status, AuditAction.REJECT)


def test_target_status_unknown_returns_none():
    assert target_status(PublishStatus.APPROVED, AuditAction.SUBMIT) is None
    assert target_status("bogus", "bogus") is None
