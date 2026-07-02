"""Tests for notification list filters."""

from integration.notifications.filters import (
    exclude_apply_result_pending,
    is_apply_result_pending,
)
from integration.notifications.normalize import normalize_kb_message


def _item(mass_type, state, msg_id=1):
    return normalize_kb_message({
        "id": msg_id,
        "massType": mass_type,
        "massage": "test",
        "state": state,
        "createTime": "2026-06-26T14:30:16.017132",
    })


def test_is_apply_result_pending_mass_type_3_state_2():
    assert is_apply_result_pending(_item(3, 2, 109)) is True


def test_is_apply_result_pending_mass_type_3_state_1():
    assert is_apply_result_pending(_item(3, 1, 100)) is False


def test_is_apply_result_pending_mass_type_1_state_2():
    assert is_apply_result_pending(_item(1, 2, 97)) is False


def test_is_apply_result_pending_mass_type_4_state_2():
    assert is_apply_result_pending(_item(4, 2, 110)) is False


def test_exclude_apply_result_pending_drops_only_pending_results():
    items = [
        _item(3, 2, 109),
        _item(3, 1, 100),
        _item(4, 2, 110),
        _item(1, 2, 97),
    ]
    kept = exclude_apply_result_pending(items)
    assert [item["ref_id"] for item in kept] == ["100", "110", "97"]
