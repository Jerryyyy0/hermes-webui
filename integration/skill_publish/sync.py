"""Query-time incremental sync of upstream audit events (docs §8.2/8.3/10.8).

Triggered from the notifications list/summary queries and the application
detail query. Pulls ``GET /api/external/notifications`` since the last seen
event id and writes back application status, audit log and skill_versions.
Notifications themselves are NOT persisted locally - the notifications API
re-pulls the event stream live at query time. Upstream failures are
swallowed (return 0) - sync must never block a user-facing query.
"""

from __future__ import annotations

import logging
from contextlib import closing

from integration.skill_publish import store
from integration.skill_publish.client import fetch_external_notifications
from integration.skill_publish.constants import (
    ApplicationType,
    AuditAction,
    OperatorRole,
    PublishStatus,
    UPSTREAM_STATUS_LISTED,
    UPSTREAM_STATUS_UNLISTED,
)
from integration.skill_publish.version_utils import parse_apply_time

_log = logging.getLogger(__name__)

_NOTIF_LIMIT = 50


def _platform() -> str:
    from integration.config import skill_publish_platform

    return skill_publish_platform()


def _last_event_id(account: str, db_path=None) -> int:
    # Cursor uses MAX(audit_event_id) across the account's applications (8.2).
    with closing(store._connect(db_path)) as conn:
        row = conn.execute(
            "SELECT MAX(audit_event_id) AS m FROM skill_application_forms"
            " WHERE submitter_account = ?",
            (account,),
        ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def sync_notifications_for_account(
    account: str, *, external_user_id: str = "", db_path=None
) -> int:
    """Incremental sync; returns count of newly applied events. Never raises."""
    if not account:
        return 0
    ext_id = external_user_id or account
    try:
        events = fetch_external_notifications(
            platform=_platform(),
            external_user_id=ext_id,
            since_id=_last_event_id(account, db_path),
            limit=_NOTIF_LIMIT,
        )
    except Exception:
        _log.debug("skill-publish sync failed for %s", account, exc_info=True)
        return 0
    applied = 0
    for event in events:
        try:
            if _apply_event(event, account, db_path=db_path):
                applied += 1
        except Exception:
            _log.exception(
                "skill-publish: failed to apply event %s", event.get("id")
            )
    return applied


def sync_application_if_pending(
    app: dict, db_path=None
) -> dict:
    """Detail-query hook (8.3): refresh a pending app if its event arrived."""
    if app.get("status") != PublishStatus.PENDING:
        return app
    account = app.get("submitter_account") or ""
    ext_id = app.get("external_user_id") or app.get("submitter_uuid") or ""
    try:
        events = fetch_external_notifications(
            platform=_platform(),
            external_user_id=ext_id,
            since_id=int(app.get("audit_event_id") or 0),
            limit=_NOTIF_LIMIT,
        )
    except Exception:
        _log.debug("skill-publish detail sync failed", exc_info=True)
        return app
    for event in events:
        if str(event.get("applicationId") or "") != str(
            app.get("upstream_application_id") or ""
        ):
            continue
        if _apply_event(event, account, db_path=db_path):
            break
    refreshed = store.get_application(app["id"], db_path=db_path)
    return refreshed or app


def _apply_event(event: dict, account: str, *, db_path=None) -> bool:
    scene = str(event.get("scene") or "")
    if scene in ("1", "2"):
        return _apply_review_event(event, account, db_path=db_path)
    if scene == "3":
        return _apply_admin_online_offline(
            event, "offline", UPSTREAM_STATUS_UNLISTED, db_path=db_path
        )
    if scene == "4":
        return _apply_admin_online_offline(
            event, "online", UPSTREAM_STATUS_LISTED, db_path=db_path
        )
    return False


def _find_pending_by_event(event: dict, db_path=None) -> dict | None:
    upstream_app_id = str(event.get("applicationId") or "")
    if not upstream_app_id:
        return None
    with closing(store._connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM skill_application_forms"
            " WHERE upstream_application_id = ? AND status = ? AND hidden IN (0, 1)",
            (upstream_app_id, PublishStatus.PENDING),
        ).fetchone()
    return store._row_to_application(row) if row else None


def _apply_review_event(event: dict, account: str, *, db_path=None) -> bool:
    app = _find_pending_by_event(event, db_path=db_path)
    if not app:
        return False
    # Strong match by application_id; name+version mismatch only warns (8.2).
    if (
        str(event.get("name") or "") != app.get("skill_name")
        or str(event.get("version") or "") != app.get("version")
    ):
        _log.warning(
            "skill-publish: event %s name/version mismatch (event=%s/%s, app=%s/%s)",
            event.get("id"), event.get("name"), event.get("version"),
            app.get("skill_name"), app.get("version"),
        )
    event_id = int(event.get("id") or 0)
    occurred_at = parse_apply_time(event.get("createTime"))
    approved = str(event.get("result")) == "1"
    comment = str(event.get("auditComment") or "")
    operator = str(event.get("operator") or "")
    operator_name = str(event.get("operatorName") or "")
    app_type = app.get("application_type") or ApplicationType.PUBLISH

    if approved:
        status_fields = {
            "status": PublishStatus.APPROVED,
            "hidden": 0,
            "audit_event_id": event_id,
            "audited_at": occurred_at,
        }
    else:
        status_fields = {
            "status": PublishStatus.REJECTED,
            "hidden": 0,
            "audit_comment": comment,
            "audit_event_id": event_id,
            "audited_at": occurred_at,
        }
    store.update_application(app["id"], fields=status_fields, db_path=db_path)

    store.insert_audit_log(
        application_id=app["id"],
        action=AuditAction.APPROVE if approved else AuditAction.REJECT,
        from_status=PublishStatus.PENDING,
        to_status=PublishStatus.APPROVED if approved else PublishStatus.REJECTED,
        operator=operator,
        operator_name=operator_name,
        operator_role=OperatorRole.AUDITOR,
        comment=comment,
        upstream_application_id=str(event.get("applicationId") or ""),
        upstream_event_id=event_id,
        created_at=occurred_at,
        db_path=db_path,
    )

    if approved and app_type == ApplicationType.PUBLISH:
        store.insert_version_and_demote(
            fields={
                "skill_name": app["skill_name"],
                "version": app["version"],
                "submitter_account": app["submitter_account"],
                "display_name": app.get("display_name") or "",
                "upstream_skill_id": app.get("upstream_skill_id") or "",
                "upstream_status": UPSTREAM_STATUS_LISTED,
                "application_id": app["id"],
                "released_at": occurred_at,
            },
            db_path=db_path,
        )
    elif approved:
        store.update_version_status(
            app["skill_name"], UPSTREAM_STATUS_UNLISTED, db_path=db_path
        )
    elif app_type == ApplicationType.UNPUBLISH:
        store.update_version_status(
            app["skill_name"], UPSTREAM_STATUS_LISTED, db_path=db_path
        )
    return True


def _apply_admin_online_offline(
    event: dict,
    action_label: str,
    upstream_status: str,
    *,
    db_path=None,
) -> bool:
    """Scene=3/4: admin offline/online - no application matched (10.8)."""
    skill_name = str(event.get("name") or "")
    if not skill_name:
        return False
    event_id = int(event.get("id") or 0)
    occurred_at = parse_apply_time(event.get("createTime"))
    store.update_version_status(skill_name, upstream_status, db_path=db_path)
    with closing(store._connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM skill_application_forms WHERE skill_name = ?"
            " AND status = ? ORDER BY audited_at DESC LIMIT 1",
            (skill_name, PublishStatus.APPROVED),
        ).fetchone()
    app = store._row_to_application(row) if row else None
    if app:
        store.insert_audit_log(
            application_id=app["id"],
            action=f"admin_{action_label}",
            from_status=PublishStatus.APPROVED,
            to_status=PublishStatus.APPROVED,
            operator=str(event.get("operator") or ""),
            operator_name=str(event.get("operatorName") or ""),
            operator_role=OperatorRole.AUDITOR,
            comment=str(event.get("auditComment") or ""),
            upstream_application_id=str(event.get("applicationId") or ""),
            upstream_event_id=event_id,
            created_at=occurred_at,
            db_path=db_path,
        )
    return True
