"""SQLite store for notifications.

HTTP handlers do not use this module yet; table schema and CRUD are reserved
for future local notification types.
"""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from api.config import STATE_DIR


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Connect to notifications.db with WAL mode."""
    if db_path is None:
        db_path = STATE_DIR / "notifications.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create notifications table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            ref_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unread',
            priority TEXT NOT NULL DEFAULT 'normal',
            actionable INTEGER NOT NULL DEFAULT 0,
            action_status TEXT DEFAULT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_category ON notifications(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at DESC)")
    conn.commit()


def create_notification(
    *,
    id: str,
    category: str,
    title: str,
    body: str = "",
    source: str = "",
    ref_id: str = "",
    status: str = "unread",
    priority: str = "normal",
    actionable: bool = False,
    action_status: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    created_at: float,
    db_path: Optional[Path] = None,
) -> str:
    """Create a new notification. Returns the notification ID."""
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        updated_at = created_at
        metadata_json = json.dumps(metadata or {})
        conn.execute(
            """
            INSERT INTO notifications (
                id, category, title, body, source, ref_id,
                status, priority, actionable, action_status,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id, category, title, body, source, ref_id,
                status, priority, 1 if actionable else 0, action_status,
                metadata_json, created_at, updated_at,
            ),
        )
        conn.commit()
        return id


def list_notifications(
    *,
    category: Optional[str] = None,
    status: Optional[str] = None,
    cursor: Optional[float] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """
    List notifications with cursor-based pagination.
    Returns up to `limit + 1` items (extra item used to determine next_cursor).
    """
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        
        conditions = []
        params: list[Any] = []
        
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        
        if cursor is not None:
            conditions.append("created_at < ?")
            params.append(cursor)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        query = f"""
            SELECT * FROM notifications
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(limit + 1)
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_notification(
    notification_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Get a single notification by ID."""
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM notifications WHERE id = ?",
            (notification_id,),
        ).fetchone()
        return dict(row) if row else None


def summary(
    recent_limit: int = 5,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Get notification summary: total count, unread count, counts by category,
    and recent unread notifications.
    """
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        
        # Total count
        total = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        
        # Unread count
        unread = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE status = 'unread'"
        ).fetchone()[0]
        
        # Counts by category
        category_rows = conn.execute("""
            SELECT category, 
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) as unread
            FROM notifications
            GROUP BY category
        """).fetchall()
        
        by_category = {
            row["category"]: {
                "total": row["total"],
                "unread": row["unread"],
            }
            for row in category_rows
        }
        
        # Recent unread notifications
        recent_rows = conn.execute("""
            SELECT id, category, title, source, created_at
            FROM notifications
            WHERE status = 'unread'
            ORDER BY created_at DESC
            LIMIT ?
        """, (recent_limit,)).fetchall()
        
        recent_unread = [dict(row) for row in recent_rows]
        
        return {
            "total": total,
            "unread": unread,
            "by_category": by_category,
            "recent_unread": recent_unread,
        }


def mark_read(
    notification_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Mark a single notification as read. Returns True if updated."""
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        updated_at = time.time()
        cursor = conn.execute(
            """
            UPDATE notifications
            SET status = 'read', updated_at = ?
            WHERE id = ?
            """,
            (updated_at, notification_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_read_batch(
    ids: list[str],
    db_path: Optional[Path] = None,
) -> int:
    """Mark multiple notifications as read. Returns count of updated records."""
    if not ids:
        return 0
    
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        updated_at = time.time()
        placeholders = ", ".join("?" * len(ids))
        cursor = conn.execute(
            f"""
            UPDATE notifications
            SET status = 'read', updated_at = ?
            WHERE id IN ({placeholders})
            """,
            [updated_at] + ids,
        )
        conn.commit()
        return cursor.rowcount


def delete_batch(
    ids: list[str],
    db_path: Optional[Path] = None,
) -> int:
    """Delete multiple notifications. Returns count of deleted records."""
    if not ids:
        return 0
    
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        placeholders = ", ".join("?" * len(ids))
        cursor = conn.execute(
            f"DELETE FROM notifications WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return cursor.rowcount
