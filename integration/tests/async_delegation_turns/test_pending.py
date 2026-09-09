from types import SimpleNamespace

from integration.async_delegation_turns.pending import public_pending_state


def test_human_pending_projection_remains_visible():
    session = SimpleNamespace(
        pending_user_message="用户输入",
        pending_attachments=[{"name": "input.txt"}],
        pending_user_source="webui",
        pending_turn_key="turn:2",
    )

    assert public_pending_state(session, include_attachments=True) == {
        "pending_user_message": "用户输入",
        "pending_attachments": [{"name": "input.txt"}],
        "pending_user_source": "webui",
        "pending_turn_key": "turn:2",
        "pending_user_visible": True,
        "has_pending_user_message": True,
    }


def test_async_wakeup_pending_projection_hides_prompt_and_attachments():
    session = SimpleNamespace(
        pending_user_message="[ASYNC DELEGATION BATCH COMPLETE — secret result]",
        pending_attachments=[{"name": "internal.txt"}],
        pending_user_source="async_delegation_wakeup",
        pending_turn_key="turn:1",
    )

    assert public_pending_state(session, include_attachments=True) == {
        "pending_user_message": None,
        "pending_attachments": [],
        "pending_user_source": "async_delegation_wakeup",
        "pending_turn_key": "turn:1",
        "pending_user_visible": False,
        "has_pending_user_message": True,
    }
