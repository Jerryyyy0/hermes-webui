"""Tests for skill_publish store (session_manifest.db tables)."""

import pytest

from integration.skill_publish import store
from integration.skill_publish.constants import PublishStatus


@pytest.fixture
def db(tmp_path):
    return tmp_path / "session_manifest.db"


def _make_app(**overrides):
    fields = {
        "skill_name": "demo-skill",
        "application_type": "publish",
        "submitter_account": "acct-a",
        "status": PublishStatus.DRAFT,
    }
    fields.update(overrides)
    return fields


def test_schema_idempotent(db):
    store.create_application(fields=_make_app(), db_path=db)
    # Every function call opens a new connection and re-runs _ensure_schema.
    app = store.create_application(
        fields=_make_app(skill_name="other"), db_path=db
    )
    assert store.get_application(app["id"], db_path=db) is not None


def test_create_application_generates_skp_id_and_defaults(db):
    app = store.create_application(fields=_make_app(), db_path=db)
    assert app["id"].startswith("skp-")
    assert app["created_at"] > 0
    assert app["updated_at"] > 0
    assert app["hidden"] == 0
    assert app["version"] == ""


def test_get_application_missing(db):
    assert store.get_application("skp-nope", db_path=db) is None


def test_update_application_sets_fields(db):
    app = store.create_application(fields=_make_app(), db_path=db)
    ok = store.update_application(
        app["id"], fields={"status": PublishStatus.PENDING, "version": "1.0.0"},
        db_path=db,
    )
    assert ok
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.PENDING
    assert refreshed["version"] == "1.0.0"
    assert refreshed["updated_at"] >= app["updated_at"]


def test_list_applications_filters(db):
    a = store.create_application(fields=_make_app(), db_path=db)
    store.create_application(
        fields=_make_app(skill_name="another-skill", submitter_account="acct-b"),
        db_path=db,
    )
    store.create_application(
        fields=_make_app(skill_name="hidden-one", status=PublishStatus.PENDING),
        db_path=db,
    )
    store.soft_hide(
        store.create_application(fields=_make_app(skill_name="gone"), db_path=db)["id"],
        db_path=db,
    )

    items, total = store.list_applications(account="acct-a", db_path=db)
    assert total == 2
    assert {i["skill_name"] for i in items} == {"demo-skill", "hidden-one"}

    items, total = store.list_applications(account="acct-a", status="pending", db_path=db)
    assert total == 1
    assert items[0]["skill_name"] == "hidden-one"

    items, total = store.list_applications(account="acct-b", db_path=db)
    assert total == 1
    assert items[0]["skill_name"] == "another-skill"

    # q search across LIKE fields
    items, total = store.list_applications(account="acct-a", q="demo", db_path=db)
    assert total == 1
    assert items[0]["id"] == a["id"]


def test_list_applications_pagination(db):
    for i in range(5):
        store.create_application(
            fields=_make_app(skill_name=f"skill-{i}"), db_path=db
        )
    items, total = store.list_applications(account="acct-a", page=2, page_size=2, db_path=db)
    assert total == 5
    assert len(items) == 2


def test_stats_for_counts_by_status_excluding_hidden(db):
    store.create_application(fields=_make_app(), db_path=db)  # draft
    store.create_application(
        fields=_make_app(skill_name="s2", status=PublishStatus.PENDING), db_path=db
    )
    store.create_application(
        fields=_make_app(skill_name="s3", status=PublishStatus.REJECTED), db_path=db
    )
    hidden_id = store.create_application(
        fields=_make_app(skill_name="s4"), db_path=db
    )["id"]
    store.soft_hide(hidden_id, db_path=db)

    stats = store.stats_for("acct-a", db_path=db)
    assert stats["total"] == 3
    assert stats["by_status"][PublishStatus.DRAFT] == 1
    assert stats["by_status"][PublishStatus.PENDING] == 1
    assert stats["by_status"][PublishStatus.REJECTED] == 1
    assert stats["by_status"][PublishStatus.APPROVED] == 0


def test_has_active_application(db):
    store.create_application(fields=_make_app(), db_path=db)
    assert store.has_active_application("acct-a", "demo-skill", "publish", db_path=db)
    assert not store.has_active_application(
        "acct-b", "demo-skill", "publish", db_path=db
    )
    assert not store.has_active_application(
        "acct-a", "demo-skill", "unpublish", db_path=db
    )
    # Terminal status is not active
    store.update_application(
        store.create_application(
            fields=_make_app(skill_name="term", status=PublishStatus.REJECTED),
            db_path=db,
        )["id"],
        fields={"status": PublishStatus.APPROVED},
        db_path=db,
    )
    assert not store.has_active_application("acct-a", "term", "publish", db_path=db)


def test_has_pending_application(db):
    app = store.create_application(fields=_make_app(), db_path=db)
    assert not store.has_pending_application("demo-skill", db_path=db)
    store.update_application(
        app["id"], fields={"status": PublishStatus.PENDING}, db_path=db
    )
    assert store.has_pending_application("demo-skill", db_path=db)
    assert not store.has_pending_application("demo-skill", exclude_id=app["id"], db_path=db)
    assert not store.has_pending_application("other-skill", db_path=db)


def test_soft_hide_vs_hard_delete(db):
    soft_id = store.create_application(fields=_make_app(), db_path=db)["id"]
    hard_id = store.create_application(
        fields=_make_app(skill_name="hard"), db_path=db
    )["id"]
    store.insert_audit_log(
        application_id=hard_id, action="submit", from_status="draft",
        to_status="pending", operator="acct-a", operator_role="submitter",
        db_path=db,
    )

    assert store.soft_hide(soft_id, db_path=db)
    assert store.get_application(soft_id, db_path=db)["hidden"] == 1

    assert store.hard_delete(hard_id, db_path=db)
    assert store.get_application(hard_id, db_path=db) is None
    assert store.list_audit_log(hard_id, db_path=db) == []


def test_insert_audit_log_dedupes_upstream_event_id(db):
    app = store.create_application(fields=_make_app(), db_path=db)
    kwargs = dict(
        application_id=app["id"], action="approve", from_status="pending",
        to_status="approved", operator="auditor", operator_role="auditor",
        upstream_event_id=42, created_at=1_000_000.0,
    )
    first = store.insert_audit_log(db_path=db, **kwargs)
    assert first is not None
    second = store.insert_audit_log(db_path=db, **kwargs)
    assert second is None
    assert len(store.list_audit_log(app["id"], db_path=db)) == 1


def test_list_audit_log_desc_order(db):
    app = store.create_application(fields=_make_app(), db_path=db)
    for i in range(3):
        store.insert_audit_log(
            application_id=app["id"], action=f"act-{i}", from_status="",
            to_status="s", operator="op", operator_role="submitter",
            created_at=1_000_000.0 + i, db_path=db,
        )
    logs = store.list_audit_log(app["id"], db_path=db)
    assert [l["action"] for l in logs] == ["act-2", "act-1", "act-0"]


def test_insert_version_and_demote(db):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a"},
        db_path=db,
    )
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.1",
                "submitter_account": "acct-a"},
        db_path=db,
    )
    latest = store.get_latest_version("demo-skill", db_path=db)
    assert latest["version"] == "1.0.1"
    assert latest["is_latest"] == 1
    items, _ = store.list_applications(account="acct-a", db_path=db)
    assert items == []  # versions are not applications
    from contextlib import closing
    with closing(store._connect(db)) as conn:
        rows = conn.execute(
            "SELECT version, is_latest FROM skill_versions ORDER BY version"
        ).fetchall()
    assert [(r["version"], r["is_latest"]) for r in rows] == [("1.0.0", 0), ("1.0.1", 1)]


def test_get_latest_version_missing(db):
    assert store.get_latest_version("nobody", db_path=db) is None


def test_update_version_status(db):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "2"},
        db_path=db,
    )
    assert store.update_version_status("demo-skill", "3", db_path=db)
    assert store.get_latest_version("demo-skill", db_path=db)["upstream_status"] == "3"
    assert not store.update_version_status("missing-skill", "3", db_path=db)


def test_list_published_skills(db):
    store.insert_version_and_demote(
        fields={"skill_name": "listed", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "2",
                "display_name": "Listed"},
        db_path=db,
    )
    store.insert_version_and_demote(
        fields={"skill_name": "unlisted", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "3"},
        db_path=db,
    )
    rows = store.list_published_skills("acct-a", db_path=db)
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "listed"
    assert rows[0]["display_name"] == "Listed"


def test_list_application_versions(db):
    submitted = store.create_application(fields=_make_app(), db_path=db)
    store.update_application(
        submitted["id"], fields={"submitted_at": 1_000_000.0, "version": "1.0.0"},
        db_path=db,
    )
    store.create_application(fields=_make_app(skill_name="never-submitted"), db_path=db)
    rows = store.list_application_versions("demo-skill", db_path=db)
    assert len(rows) == 1
    assert rows[0]["version"] == "1.0.0"


def test_list_pending_applications_including_hidden(db):
    """Test that list_pending_applications_including_hidden returns both hidden and non-hidden pending apps."""
    # Create a pending app with hidden=0
    app1 = store.create_application(
        fields=_make_app(status=PublishStatus.PENDING, skill_name="skill-a"),
        db_path=db,
    )
    # Create a pending app with hidden=1 (soft-deleted)
    app2 = store.create_application(
        fields=_make_app(status=PublishStatus.PENDING, skill_name="skill-b"),
        db_path=db,
    )
    store.soft_hide(app2["id"], db_path=db)
    # Create a draft app (should not be included)
    store.create_application(
        fields=_make_app(status=PublishStatus.DRAFT, skill_name="skill-c"),
        db_path=db,
    )

    result = store.list_pending_applications_including_hidden(db_path=db)
    assert len(result) == 2
    skill_names = {app["skill_name"] for app in result}
    assert "skill-a" in skill_names
    assert "skill-b" in skill_names
    assert "skill-c" not in skill_names
