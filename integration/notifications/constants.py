"""Notification system constants."""

# Downstream get_user_messages state → unified action_status (docs §6.1).
KB_STATE_TO_ACTION_STATUS: dict[int, str] = {
    0: "rejected",
    1: "approved",
    2: "pending",
}

# massType values for which the client may open a detail view (join requests).
KB_OPENABLE_MASS_TYPES = frozenset({1})

VALID_READ_TYPES = frozenset({"all", "unread", "seen"})


class KbMassType:
    APPLY = 1
    MEMBER_LEFT = 2
    APPLY_RESULT = 3


class NotificationCategory:
    KB_APPLY = "kb_apply"
    SKILL_PUBLISH = "skill_publish"


class NotificationIdPrefix:
    KB = "kb:"
    SKILL_PUBLISH = "skill_publish:"


class NotificationStatus:
    UNREAD = "unread"
    READ = "read"


class NotificationPriority:
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class KbActionStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    IGNORED = "ignored"

    TERMINAL = frozenset({APPROVED, REJECTED, IGNORED})
