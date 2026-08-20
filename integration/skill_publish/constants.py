"""Enums and shared constants for the skill publish flow."""

from __future__ import annotations


class PublishStatus:
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = (DRAFT, PENDING, APPROVED, REJECTED)
    # States allowed for (re-)submit (7.5 step 1).
    SUBMITTABLE = (DRAFT, REJECTED)


class ApplicationType:
    PUBLISH = "publish"
    UNPUBLISH = "unpublish"

    ALL = (PUBLISH, UNPUBLISH)


class AuditAction:
    SUBMIT = "submit"
    RE_SUBMIT = "re_submit"
    WITHDRAW = "withdraw"
    APPROVE = "approve"
    REJECT = "reject"


class OperatorRole:
    SUBMITTER = "submitter"
    AUDITOR = "auditor"
    SYSTEM = "system"


# Upstream skill status codes (10.3).
UPSTREAM_STATUS_PENDING_REVIEW = "1"
UPSTREAM_STATUS_LISTED = "2"
UPSTREAM_STATUS_UNLISTED = "3"
UPSTREAM_STATUS_PENDING_UNPUBLISH = "4"

APPLICATION_ID_PREFIX = "skp-"
DEFAULT_PLATFORM = "hermes-webui"
