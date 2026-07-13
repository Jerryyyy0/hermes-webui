from integration.session_status.compute import compute_session_status


def test_status_precedence_and_unread_are_independent():
    row = {
        "last_message_at": 20,
        "last_error_at": 30,
        "is_streaming": True,
    }
    assert compute_session_status(row, 10) == {"status": "error", "is_unread": True}
    assert compute_session_status(row, 30) == {"status": "error", "is_unread": False}


def test_runtime_pending_and_idle_statuses():
    assert compute_session_status({"pending_user_message": "hello"}, 0)["status"] == "in_progress"
    assert compute_session_status({"last_message_at": 20}, 10) == {
        "status": "has_new_messages",
        "is_unread": True,
    }
    assert compute_session_status({"last_message_at": 20}, 20) == {
        "status": "ready",
        "is_unread": False,
    }


def test_invalid_timestamps_do_not_use_similar_fields():
    row = {"last_message_at": "invalid", "last_error_at": -1, "updated_at": 999}
    assert compute_session_status(row, 0) == {"status": "ready", "is_unread": False}
