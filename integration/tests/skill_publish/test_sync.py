"""Tests for skill_publish query-time incremental sync.

Notifications are NOT written locally (upstream-owned); sync only updates
application state, audit log and skill_versions.
"""

from unittest.mock import patch

import pytest

from integration.skill_publish import store, sync
from integration.skill_publish.constants import PublishStatus


@pytest.fixture
def db(tmp_path):
    return tmp_path / "session_manifest.db"


def _make_pending_app(db, **overrides):
    fields = {
        "skill_name": "demo-skill",
        "display_name": "Demo Skill",
        "application_type": "publish",
        "version": "1.0.0",
        "status": PublishStatus.PENDING,
        "submitter_account": "acct-a",
        "submitter_uuid": "uuid-a",
        "upstream_application_id": "up-1",
    }
    fields.update(overrides)
    return store.create_application(fields=fields, db_path=db)


def _event(event_id=10, scene="1", result="1", application_id="up-1",
           name="demo-skill", version="1.0.0", audit_comment=""):
    return {
        "id": event_id,
        "scene": scene,
        "result": result,
        "applicationId": application_id,
        "name": name,
        "version": version,
        "operator": "auditor",
        "operatorName": "审核员",
        "auditComment": audit_comment,
        "createTime": "2026-08-17 10:00:00",
    }


def _run(events, account="acct-a", db=None):
    with patch.object(sync, "fetch_external_notifications", return_value=events):
        with patch.object(sync, "_platform", return_value="test-platform"):
            return sync.sync_notifications_for_account(account, db_path=db)


def test_approved_event_updates_status_and_version(db):
    app = _make_pending_app(db)
    applied = _run([_event()], db=db)
    assert applied == 1

    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.APPROVED
    assert refreshed["audit_event_id"] == 10
    assert refreshed["audited_at"] is not None

    latest = store.get_latest_version("demo-skill", db_path=db)
    assert latest["version"] == "1.0.0"
    assert latest["upstream_status"] == "2"
    assert latest["application_id"] == app["id"]

    logs = store.list_audit_log(app["id"], db_path=db)
    assert [l["action"] for l in logs] == ["approve"]


def test_rejected_event_records_comment_and_no_version(db):
    app = _make_pending_app(db)
    applied = _run([_event(result="0", audit_comment="不符合规范")], db=db)
    assert applied == 1

    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.REJECTED
    assert refreshed["audit_comment"] == "不符合规范"

    assert store.get_latest_version("demo-skill", db_path=db) is None


def test_hidden_pending_app_is_restored_on_approval(db):
    app = _make_pending_app(db, hidden=1)
    _run([_event()], db=db)
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["hidden"] == 0


def test_sync_is_idempotent(db):
    _make_pending_app(db)
    assert _run([_event()], db=db) == 1
    # Replaying the same event: no pending app remains -> skipped.
    assert _run([_event()], db=db) == 0
    logs_count = None
    for app_id_rows in [store.list_applications(account="acct-a", db_path=db)[0]]:
        for row in app_id_rows:
            logs_count = len(store.list_audit_log(row["id"], db_path=db))
    assert logs_count == 1


def test_unpublish_approval_unlists_version(db):
    app = _make_pending_app(
        db, application_type="unpublish", upstream_application_id="up-2"
    )
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "2"},
        db_path=db,
    )
    assert _run([_event(application_id="up-2")], db=db) == 1
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.APPROVED
    assert store.get_latest_version("demo-skill", db_path=db)["upstream_status"] == "3"


def test_unpublish_rejection_restores_version_status(db):
    app = _make_pending_app(
        db, application_type="unpublish", upstream_application_id="up-ub"
    )
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "4"},
        db_path=db,
    )
    assert _run(
        [_event(result="0", application_id="up-ub", audit_comment="理由不充分")],
        db=db,
    ) == 1
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.REJECTED
    assert refreshed["audit_comment"] == "理由不充分"
    assert store.get_latest_version("demo-skill", db_path=db)["upstream_status"] == "2"


def test_name_version_mismatch_still_applies(db):
    app = _make_pending_app(db)
    applied = _run([_event(name="other-name")], db=db)
    assert applied == 1
    assert store.get_application(app["id"], db_path=db)["status"] == PublishStatus.APPROVED


def test_no_matching_application_skips(db):
    _make_pending_app(db)
    assert _run([_event(application_id="up-unknown")], db=db) == 0


def test_upstream_failure_returns_zero(db):
    _make_pending_app(db)
    with patch.object(
        sync, "fetch_external_notifications", side_effect=RuntimeError("down")
    ):
        with patch.object(sync, "_platform", return_value="p"):
            assert sync.sync_notifications_for_account("acct-a", db_path=db) == 0


def test_empty_account_returns_zero(db):
    assert sync.sync_notifications_for_account("", db_path=db) == 0


def test_since_id_uses_max_audit_event_id(db):
    app = _make_pending_app(db)
    store.update_application(app["id"], fields={"audit_event_id": 7}, db_path=db)
    with patch.object(
        sync, "fetch_external_notifications", return_value=[]
    ) as mock_fetch:
        with patch.object(sync, "_platform", return_value="p"):
            sync.sync_notifications_for_account("acct-a", db_path=db)
    assert mock_fetch.call_args.kwargs["since_id"] == 7


def test_admin_offline_event_scene3(db):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "2",
                "application_id": "app-1"},
        db_path=db,
    )
    approved = _make_pending_app(db, status=PublishStatus.APPROVED)
    applied = _run([_event(scene="3", application_id="", event_id=20)], db=db)
    assert applied == 1
    assert store.get_latest_version("demo-skill", db_path=db)["upstream_status"] == "3"
    logs = store.list_audit_log(approved["id"], db_path=db)
    assert [l["action"] for l in logs] == ["admin_offline"]


def test_admin_online_event_scene4(db):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "3"},
        db_path=db,
    )
    assert _run([_event(scene="4", application_id="", event_id=21)], db=db) == 1
    assert store.get_latest_version("demo-skill", db_path=db)["upstream_status"] == "2"


def test_sync_application_if_pending_refreshes(db):
    app = _make_pending_app(db)
    with patch.object(
        sync, "fetch_external_notifications", return_value=[_event()]
    ):
        with patch.object(sync, "_platform", return_value="p"):
            refreshed = sync.sync_application_if_pending(app, db_path=db)
    assert refreshed["status"] == PublishStatus.APPROVED


def test_sync_application_if_pending_skips_non_pending(db):
    app = _make_pending_app(db, status=PublishStatus.APPROVED)
    with patch.object(sync, "fetch_external_notifications") as mock_fetch:
        refreshed = sync.sync_application_if_pending(app, db_path=db)
    mock_fetch.assert_not_called()
    assert refreshed["status"] == PublishStatus.APPROVED
