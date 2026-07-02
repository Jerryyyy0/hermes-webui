"""Tests for KB notification normalization."""

from integration.notifications.normalize import (
    is_actionable,
    map_kb_read_status,
    normalize_kb_message,
)


def test_mass_type_1_pending_is_actionable():
    msg = {
        "id": 97,
        "massType": 1,
        "massage": "用户 chenyuxin 申请加入您的 学习资料 知识库",
        "showName": "学习资料",
        "state": 2,
        "createTime": "2026-06-26T14:30:16.017132",
    }
    item = normalize_kb_message(msg)
    assert item["actionable"] == 1
    assert item["action_status"] == "pending"
    assert "actions" not in item


def test_mass_type_1_rejected_maps_action_status():
    msg = {
        "id": 99,
        "massType": 1,
        "massage": "已拒绝",
        "state": 0,
        "createTime": "2026-06-26T14:30:16.017132",
    }
    item = normalize_kb_message(msg)
    assert item["actionable"] == 1
    assert item["action_status"] == "rejected"


def test_mass_type_1_approved_stays_actionable():
    msg = {
        "id": 98,
        "massType": 1,
        "massage": "用户 chenyuxin 申请加入您的 学习资料 知识库",
        "state": 1,
        "createTime": "2026-06-26T14:30:16.017132",
    }
    item = normalize_kb_message(msg)
    assert item["actionable"] == 1
    assert item["action_status"] == "approved"
    assert "actions" not in item


def test_mass_type_3_apply_result_not_actionable():
    msg = {
        "id": 100,
        "massType": 3,
        "massage": "您申请加入 学习资料 知识库的申请已通过",
        "state": 1,
        "createTime": "2026-06-30T16:33:06.253961",
        "showName": "学习资料",
    }
    item = normalize_kb_message(msg)
    assert item["actionable"] == 0
    assert item["action_status"] == ""
    assert "actions" not in item


def test_mass_type_2_member_left_not_actionable():
    msg = {
        "id": 67,
        "massType": 2,
        "massage": "用户 田佩佩 (tianpeipei) 已主动退出您的 测试上传文件 知识库",
        "showName": "测试上传文件",
        "state": 2,
        "createTime": "2026-02-03T16:42:36.227037",
    }
    item = normalize_kb_message(msg)
    assert item["actionable"] == 0
    assert item["action_status"] == ""
    assert "actions" not in item


def test_missing_mass_type_not_actionable():
    msg = {"id": 1, "massage": "legacy", "state": 0}
    assert is_actionable(msg) == 0


def test_read_type_unread_maps_status():
    msg = {"id": 97, "massType": 1, "massage": "x", "state": 2}
    item = normalize_kb_message(msg, read_type="unread")
    assert item["status"] == "unread"


def test_read_type_seen_maps_status():
    msg = {"id": 97, "massType": 1, "massage": "x", "state": 2}
    item = normalize_kb_message(msg, read_type="seen")
    assert item["status"] == "read"


def test_read_type_all_uses_seen_ids():
    msg = {"id": 97, "massType": 1, "massage": "x", "state": 2}
    assert map_kb_read_status(msg, read_type="all", seen_ids={"97"}) == "read"
    assert map_kb_read_status(msg, read_type="all", seen_ids=set()) == "unread"
    item = normalize_kb_message(msg, read_type="all", seen_ids={"97"})
    assert item["status"] == "read"
