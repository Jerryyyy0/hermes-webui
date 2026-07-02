"""Tests for notification store module."""

import time
import unittest
from pathlib import Path

from integration.notifications import store
from integration.notifications.constants import NotificationCategory, NotificationStatus

TEST_CATEGORY = "test_event"


class TestNotificationStore(unittest.TestCase):
    """Test cases for notification store functions."""

    def setUp(self):
        """Set up test database."""
        self.db_path = Path("/tmp/test_notifications.db")
        if self.db_path.exists():
            self.db_path.unlink()

    def tearDown(self):
        """Clean up test database."""
        if self.db_path.exists():
            self.db_path.unlink()

    def test_create_notification(self):
        """Test creating a notification."""
        notif_id = store.create_notification(
            id="test-1",
            category=TEST_CATEGORY,
            title="测试通知",
            body="正文",
            source="test",
            ref_id="ref-123",
            created_at=time.time(),
            db_path=self.db_path,
        )

        self.assertEqual(notif_id, "test-1")

        notif = store.get_notification("test-1", db_path=self.db_path)
        self.assertIsNotNone(notif)
        self.assertEqual(notif["category"], TEST_CATEGORY)
        self.assertEqual(notif["title"], "测试通知")
        self.assertEqual(notif["status"], NotificationStatus.UNREAD)

    def test_list_notifications_with_cursor(self):
        """Test listing notifications with cursor-based pagination."""
        base_time = time.time()

        for i in range(5):
            store.create_notification(
                id=f"test-{i}",
                category=TEST_CATEGORY,
                title=f"通知 {i}",
                created_at=base_time - i,
                db_path=self.db_path,
            )

        items = store.list_notifications(limit=2, db_path=self.db_path)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["id"], "test-0")
        self.assertEqual(items[1]["id"], "test-1")

        cursor = items[1]["created_at"]
        items2 = store.list_notifications(limit=2, cursor=cursor, db_path=self.db_path)
        self.assertEqual(len(items2), 3)
        self.assertEqual(items2[0]["id"], "test-2")
        self.assertEqual(items2[1]["id"], "test-3")

    def test_list_notifications_filter_by_category(self):
        """Test filtering notifications by category."""
        base_time = time.time()

        store.create_notification(
            id="event-1",
            category=TEST_CATEGORY,
            title="事件 A",
            created_at=base_time,
            db_path=self.db_path,
        )

        store.create_notification(
            id="kb-1",
            category=NotificationCategory.KB_APPLY,
            title="知识库申请",
            created_at=base_time - 1,
            db_path=self.db_path,
        )

        items = store.list_notifications(
            category=TEST_CATEGORY,
            db_path=self.db_path,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "event-1")

    def test_mark_read(self):
        """Test marking notification as read."""
        store.create_notification(
            id="test-1",
            category=TEST_CATEGORY,
            title="测试",
            created_at=time.time(),
            db_path=self.db_path,
        )

        notif = store.get_notification("test-1", db_path=self.db_path)
        self.assertEqual(notif["status"], NotificationStatus.UNREAD)

        store.mark_read("test-1", db_path=self.db_path)

        notif = store.get_notification("test-1", db_path=self.db_path)
        self.assertEqual(notif["status"], NotificationStatus.READ)

    def test_mark_read_batch(self):
        """Test batch marking notifications as read."""
        for i in range(3):
            store.create_notification(
                id=f"test-{i}",
                category=TEST_CATEGORY,
                title=f"测试 {i}",
                created_at=time.time(),
                db_path=self.db_path,
            )

        store.mark_read_batch(["test-0", "test-1"], db_path=self.db_path)

        notif0 = store.get_notification("test-0", db_path=self.db_path)
        notif1 = store.get_notification("test-1", db_path=self.db_path)
        notif2 = store.get_notification("test-2", db_path=self.db_path)

        self.assertEqual(notif0["status"], NotificationStatus.READ)
        self.assertEqual(notif1["status"], NotificationStatus.READ)
        self.assertEqual(notif2["status"], NotificationStatus.UNREAD)

    def test_delete_batch(self):
        """Test batch deleting notifications."""
        for i in range(3):
            store.create_notification(
                id=f"test-{i}",
                category=TEST_CATEGORY,
                title=f"测试 {i}",
                created_at=time.time(),
                db_path=self.db_path,
            )

        store.delete_batch(["test-0", "test-1"], db_path=self.db_path)

        notif0 = store.get_notification("test-0", db_path=self.db_path)
        notif1 = store.get_notification("test-1", db_path=self.db_path)
        notif2 = store.get_notification("test-2", db_path=self.db_path)

        self.assertIsNone(notif0)
        self.assertIsNone(notif1)
        self.assertIsNotNone(notif2)

    def test_summary(self):
        """Test getting notification summary."""
        base_time = time.time()

        for i in range(3):
            store.create_notification(
                id=f"event-{i}",
                category=TEST_CATEGORY,
                title=f"事件 {i}",
                created_at=base_time - i,
                db_path=self.db_path,
            )

        for i in range(2):
            store.create_notification(
                id=f"kb-{i}",
                category=NotificationCategory.KB_APPLY,
                title=f"知识库 {i}",
                created_at=base_time - 10 - i,
                db_path=self.db_path,
            )

        store.mark_read("event-0", db_path=self.db_path)

        summary = store.summary(recent_limit=3, db_path=self.db_path)

        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["unread"], 4)

        self.assertEqual(summary["by_category"][TEST_CATEGORY]["total"], 3)
        self.assertEqual(summary["by_category"][TEST_CATEGORY]["unread"], 2)
        self.assertEqual(summary["by_category"][NotificationCategory.KB_APPLY]["total"], 2)
        self.assertEqual(summary["by_category"][NotificationCategory.KB_APPLY]["unread"], 2)

        self.assertEqual(len(summary["recent_unread"]), 3)
        self.assertEqual(summary["recent_unread"][0]["id"], "event-1")
        self.assertEqual(summary["recent_unread"][1]["id"], "event-2")


if __name__ == "__main__":
    unittest.main()
