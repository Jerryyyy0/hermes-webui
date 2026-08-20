"""HTTP handlers for skill publish application routes (/api/skillhub/publish/*).

Path layout (docs §7): ``{glm-5.3_common}`` segments are application IDs
(``skp-`` prefix). Literal paths match before the id-capturing regex.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from api.helpers import bad, j
from integration.config import skill_publish_enabled
from integration.skill_publish import client, store, zip_pack
from integration.skill_publish.client import (
    SkillHubConflictError,
    SkillHubUpstreamError,
)
from integration.skill_publish.constants import (
    ApplicationType,
    AuditAction,
    OperatorRole,
    PublishStatus,
    UPSTREAM_STATUS_LISTED,
    UPSTREAM_STATUS_PENDING_REVIEW,
    UPSTREAM_STATUS_PENDING_UNPUBLISH,
)
from integration.skill_publish.sync import sync_application_if_pending
from integration.skill_publish.version_utils import (
    compute_next_version,
    now,
    parse_apply_time,
)

# Test injection point: point at a temp DB (None -> STATE_DIR/session_manifest.db).
_DB_PATH: Path | str | None = None

_APPS_PREFIX = "/api/skillhub/publish/applications"
_DETAIL_RE = re.compile(r"^" + re.escape(_APPS_PREFIX) + r"/([^/]+)$")
_SUBMIT_RE = re.compile(r"^" + re.escape(_APPS_PREFIX) + r"/([^/]+)/submit$")
_WITHDRAW_RE = re.compile(r"^" + re.escape(_APPS_PREFIX) + r"/([^/]+)/withdraw$")
_VERSIONS_RE = re.compile(r"^" + re.escape(_APPS_PREFIX) + r"/([^/]+)/versions$")


def _respond(handler, payload, status: int = 200) -> bool:
    j(handler, payload, status=status)
    return True


def _respond_bad(handler, msg, status: int = 400) -> bool:
    bad(handler, msg, status=status)
    return True


def _qs(parsed) -> dict[str, str]:
    qs = parse_qs(parsed.query or "")
    return {k: (v[0] if v else "") for k, v in qs.items()}


def try_handle_get(handler, parsed) -> bool:
    if not skill_publish_enabled():
        return False
    path = parsed.path
    qs = _qs(parsed)
    if path == _APPS_PREFIX:
        return _get_list(handler, qs)
    if path == "/api/skillhub/publish/my-published-skills":
        return _get_my_published(handler, qs)
    if path == "/api/skillhub/publish/skill-versions":
        return _get_skill_versions(handler, qs)
    match = _VERSIONS_RE.match(path)
    if match:
        return _get_application_versions(handler, qs, match.group(1))
    match = _DETAIL_RE.match(path)
    if match:
        return _get_detail(handler, qs, match.group(1))
    return False


def try_handle_post(handler, parsed, body: dict) -> bool:
    if not skill_publish_enabled():
        return False
    path = parsed.path
    if path == _APPS_PREFIX:
        return _post_create(handler, body or {})
    match = _SUBMIT_RE.match(path)
    if match:
        return _post_submit(handler, match.group(1), body or {})
    match = _WITHDRAW_RE.match(path)
    if match:
        return _post_withdraw(handler, match.group(1), body or {})
    return False


def try_handle_delete(handler, parsed) -> bool:
    if not skill_publish_enabled():
        return False
    match = _DETAIL_RE.match(parsed.path)
    if match:
        return _delete_application(handler, _qs(parsed), match.group(1))
    return False


# ── GET ──────────────────────────────────────────────────────────────────────


def _get_list(handler, qs: dict[str, str]) -> bool:
    try:
        page = max(int(qs.get("page") or 1), 1)
        page_size = min(max(int(qs.get("page_size") or 20), 1), 50)
    except ValueError:
        return _respond_bad(handler, "分页参数无效", 400)
    status = (qs.get("status") or "").strip()
    if status and status not in PublishStatus.ALL:
        return _respond_bad(handler, "状态参数无效", 400)
    # Sync pending applications before listing (8.4).
    _sync_pending_applications()
    items, total = store.list_applications(
        q=(qs.get("q") or "").strip() or None,
        application_type=(qs.get("application_type") or "").strip() or None,
        status=status or None,
        page=page,
        page_size=page_size,
        db_path=_DB_PATH,
    )
    is_first_map = store.compute_is_first_for_apps(items, db_path=_DB_PATH)
    return _respond(
        handler,
        {
            "items": [_list_item(a, is_first_map.get(a.get("id") or "", False)) for a in items],
            "total": total,
            "page": page,
            "page_size": page_size,
            "stats": store.stats_for(db_path=_DB_PATH),
        },
    )


def _sync_pending_applications() -> None:
    """Sync all pending applications with upstream (8.4).

    Includes hidden=1 (soft-deleted) applications so that if the admin
    approves/rejects after user deletion, the status and hidden flag are
    updated correctly.
    """
    try:
        pending_apps = store.list_pending_applications_including_hidden(
            db_path=_DB_PATH,
        )
        for app in pending_apps:
            try:
                sync_application_if_pending(app, db_path=_DB_PATH)
            except Exception:
                pass  # Sync failure should not block listing
    except Exception:
        pass  # Sync failure should not block listing


def _get_detail(handler, qs: dict[str, str], app_id: str) -> bool:
    app = store.get_application(app_id, db_path=_DB_PATH)
    if not app:
        return _respond_bad(handler, "申请单不存在", 404)
    app = sync_application_if_pending(app, db_path=_DB_PATH)
    payload = dict(app)
    payload["tags"] = _parse_json_field(app.get("tags"), [])
    payload["detail_json"] = _parse_json_field(app.get("detail_json"), None)
    audit_log = store.list_audit_log(app_id, db_path=_DB_PATH)
    return _respond(handler, {"application": payload, "audit_log": audit_log})


def _get_application_versions(handler, qs: dict[str, str], app_id: str) -> bool:
    skill_name = (qs.get("skill_name") or "").strip()
    if not skill_name:
        return _respond_bad(handler, "缺少 skill_name 参数", 400)
    latest = store.get_latest_version(skill_name, db_path=_DB_PATH)
    apps = store.list_application_versions(skill_name, db_path=_DB_PATH)
    return _respond(
        handler,
        {
            "skill_name": skill_name,
            "latest_version": (latest or {}).get("version") or "",
            "versions": [
                {
                    "version": a.get("version") or "",
                    "application_id": a.get("id"),
                    "status": a.get("status"),
                    "application_type": a.get("application_type"),
                    "submitted_at": a.get("submitted_at"),
                    "audited_at": a.get("audited_at"),
                    "audit_comment": a.get("audit_comment") or "",
                }
                for a in apps
            ],
        },
    )


def _get_my_published(handler, qs: dict[str, str]) -> bool:
    rows = store.list_published_skills(db_path=_DB_PATH)
    return _respond(
        handler,
        {
            "items": [
                {
                    "skill_name": r.get("skill_name"),
                    "display_name": r.get("display_name") or "",
                    "latest_version": r.get("version") or "",
                    "upstream_status": r.get("upstream_status") or "",
                    "last_application_id": r.get("application_id") or "",
                }
                for r in rows
            ]
        },
    )


def _get_skill_versions(handler, qs: dict[str, str]) -> bool:
    skill_name = (qs.get("skill_name") or "").strip()
    if not skill_name:
        return _respond_bad(handler, "缺少 skill_name 参数", 400)
    latest = store.get_latest_version(skill_name, db_path=_DB_PATH)
    if not latest:
        return _respond(
            handler,
            {"skill_name": skill_name, "exists": False, "next_version": "1.0.0"},
        )
    return _respond(
        handler,
        {
            "skill_name": skill_name,
            "exists": True,
            "latest_version": latest.get("version") or "",
            "next_version": compute_next_version(latest.get("version")),
            "last_application_id": latest.get("application_id") or "",
            "last_submitted_at": latest.get("released_at"),
        },
    )


# ── POST create ──────────────────────────────────────────────────────────────


def _post_create(handler, body: dict) -> bool:
    skill_name = str(body.get("skill_name") or "").strip()
    account = str(body.get("account") or "").strip()
    uuid = str(body.get("uuid") or "").strip()
    app_type = str(body.get("application_type") or ApplicationType.PUBLISH).strip()
    reason = str(body.get("reason") or "").strip()
    if not skill_name:
        return _respond_bad(handler, "缺少 skill_name", 400)
    if not account:
        return _respond_bad(handler, "缺少用户标识 account", 400)
    if app_type not in ApplicationType.ALL:
        return _respond_bad(handler, "申请类型无效，仅支持 publish（上架）或 unpublish（下架）", 400)
    if app_type == ApplicationType.UNPUBLISH and not reason:
        return _respond_bad(handler, "下架申请必须填写 reason", 400)
    if store.has_active_application(
        account, skill_name, app_type, db_path=_DB_PATH
    ):
        return _respond_bad(
            handler, f"技能 '{skill_name}' 已存在进行中的申请（草稿或审核中），请勿重复提交", 409
        )

    fields: dict[str, Any] = {
        "id": "",
        "skill_name": skill_name,
        "application_type": app_type,
        "reason": reason,
        "status": PublishStatus.DRAFT,
        "version": "",
        "submitter_account": account,
        "submitter_uuid": uuid,
        "external_user_id": uuid,
        "hidden": 0,
    }

    if app_type == ApplicationType.PUBLISH:
        snapshot = _read_local_skill_snapshot(skill_name)
        if snapshot is None:
            return _respond_bad(handler, "本地技能不存在，请先上传技能后再申请发布", 404)
        fields.update(snapshot)
    else:
        latest = store.get_latest_version(skill_name, db_path=_DB_PATH)
        if not latest or latest.get("submitter_account") != account:
            return _respond_bad(handler, "无权下架该技能", 403)
        if latest.get("upstream_status") != "2":
            return _respond_bad(handler, "技能非上架状态，不可下架", 409)
        fields["version"] = latest.get("version") or ""
        fields["display_name"] = latest.get("display_name") or ""
        src_app_id = latest.get("application_id") or ""
        if src_app_id:
            src_app = store.get_application(src_app_id, db_path=_DB_PATH)
            if src_app:
                fields.update(
                    {
                        "description": src_app.get("description") or "",
                        "display_description": src_app.get("display_description") or "",
                        "category": src_app.get("category") or "",
                        "tags": src_app.get("tags") or "[]",
                        "detail_json": src_app.get("detail_json") or "",
                        "skill_md_content": src_app.get("skill_md_content") or "",
                    }
                )

    app = store.create_application(fields=fields, db_path=_DB_PATH)
    return _respond(handler, {"ok": True, "application": _public_app(app)})


def _read_local_skill_snapshot(skill_name: str) -> dict[str, Any] | None:
    from integration.skills.local_skills import _find_skill_in_any_profile, read_detail_json

    skill_dir, skill_md = _find_skill_in_any_profile(skill_name)
    if not skill_dir or not skill_md:
        return None
    try:
        md_content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    detail = read_detail_json(skill_dir) or {}
    detail_json_obj = detail.get("detail_json")
    return {
        "display_name": str(detail.get("display_name") or ""),
        "display_description": str(detail.get("display_description") or ""),
        "description": _frontmatter_description(md_content),
        "category": str(detail.get("category") or ""),
        "tags": json.dumps(detail.get("tags") or [], ensure_ascii=False),
        "detail_json": (
            json.dumps(detail_json_obj, ensure_ascii=False)
            if isinstance(detail_json_obj, (dict, list))
            else ""
        ),
        "skill_md_content": md_content,
    }


def _frontmatter_description(md_content: str) -> str:
    if not md_content.startswith("---"):
        return ""
    try:
        fm, _ = _parse_frontmatter(md_content[:4000])
        if isinstance(fm, dict):
            return str(fm.get("description") or "")
    except Exception:
        pass
    return ""


def _parse_frontmatter(text: str):
    from tools.skills_tool import _parse_frontmatter as parse

    return parse(text)


# ── POST submit ──────────────────────────────────────────────────────────────


def _post_submit(handler, app_id: str, body: dict) -> bool:
    app = store.get_application(app_id, db_path=_DB_PATH)
    if not app:
        return _respond_bad(handler, "申请单不存在", 404)
    account = str(body.get("account") or "").strip()
    if not account:
        return _respond_bad(handler, "缺少用户标识 account", 400)
    if app.get("submitter_account") != account:
        return _respond_bad(handler, "无权操作该申请单", 403)
    if app.get("status") not in PublishStatus.SUBMITTABLE:
        return _respond_bad(handler, "当前状态不允许提交审核", 409)
    if store.has_pending_application(
        app["skill_name"], exclude_id=app_id, db_path=_DB_PATH
    ):
        return _respond_bad(handler, f"技能 '{app['skill_name']}' 已有审核中的申请，请等待审核完成后再提交", 409)

    app_type = app.get("application_type") or ApplicationType.PUBLISH
    previous_status = app.get("status") or PublishStatus.DRAFT
    is_resubmit = previous_status == PublishStatus.REJECTED

    # Ownership guard for non-first publish (9.3.2).
    latest = store.get_latest_version(app["skill_name"], db_path=_DB_PATH)
    if app_type == ApplicationType.PUBLISH and latest:
        if latest.get("submitter_account") != account:
            return _respond_bad(handler, "无权更新该技能", 403)

    platform = _platform()
    version = (
        app.get("version")
        if app_type == ApplicationType.UNPUBLISH
        else compute_next_version((latest or {}).get("version"))
    )

    update_fields: dict[str, Any] = {"version": version}
    if app_type == ApplicationType.PUBLISH:
        snapshot = _read_local_skill_snapshot(app["skill_name"])
        if snapshot is None:
            return _respond_bad(handler, "本地技能不存在，请先上传技能后再申请发布", 404)
        update_fields.update(snapshot)

    tmp_zip: Path | None = None
    try:
        if app_type == ApplicationType.PUBLISH:
            skill_dir, _ = _find_skill_safe(app["skill_name"])
            if not skill_dir:
                return _respond_bad(handler, "本地技能不存在，请先上传技能后再申请发布", 404)
            tmp_zip = zip_pack.create_temp_zip_path()
            zip_pack.build_skill_zip(skill_dir, tmp_zip)

        update_fields.update(
            {"status": PublishStatus.PENDING, "submitted_at": now()}
        )
        store.update_application(app_id, fields=update_fields, db_path=_DB_PATH)

        if app_type == ApplicationType.PUBLISH:
            assert tmp_zip is not None
            resp = client.upload_skill(
                tmp_zip,
                f"{app['skill_name']}-{version}.zip",
                platform=platform,
                external_user_id=app.get("external_user_id") or app.get("submitter_uuid") or "",
                version=version,
                display_name=update_fields.get("display_name") or "",
                display_description=update_fields.get("display_description") or "",
                detail_json=update_fields.get("detail_json") or "",
                applicant_name=str(body.get("applicant_name") or ""),
                applicant_org=str(body.get("applicant_org") or ""),
                applicant_title=str(body.get("applicant_title") or ""),
                source="user",
                category=update_fields.get("category") or "",
            )
        else:
            resp = client.unpublish_skill(
                app["skill_name"],
                platform=platform,
                external_user_id=app.get("external_user_id") or app.get("submitter_uuid") or "",
                reason=app.get("reason") or "",
            )

        submitted_at = parse_apply_time(resp.get("applyTime")) or now()
        pending_status = (
            UPSTREAM_STATUS_PENDING_UNPUBLISH
            if app_type == ApplicationType.UNPUBLISH
            else UPSTREAM_STATUS_PENDING_REVIEW
        )
        upstream_fields: dict[str, Any] = {
            "submitted_at": submitted_at,
            "applicant_name": str(body.get("applicant_name") or ""),
            "applicant_org": str(body.get("applicant_org") or ""),
            "applicant_title": str(body.get("applicant_title") or ""),
            "upstream_application_id": str(resp.get("applicationId") or ""),
            "upstream_status": str(resp.get("status") or "") or pending_status,
        }
        if resp.get("name"):
            upstream_fields["upstream_name"] = str(resp["name"])
        if resp.get("skillId"):
            upstream_fields["upstream_skill_id"] = str(resp["skillId"])
        store.update_application(app_id, fields=upstream_fields, db_path=_DB_PATH)
        if app_type == ApplicationType.UNPUBLISH:
            store.update_version_status(
                app["skill_name"], UPSTREAM_STATUS_PENDING_UNPUBLISH, db_path=_DB_PATH
            )
        store.insert_audit_log(
            application_id=app_id,
            action=AuditAction.RE_SUBMIT if is_resubmit else AuditAction.SUBMIT,
            from_status=previous_status,
            to_status=PublishStatus.PENDING,
            operator=account,
            operator_role=OperatorRole.SUBMITTER,
            upstream_application_id=upstream_fields["upstream_application_id"],
            created_at=submitted_at,
            db_path=_DB_PATH,
        )
        refreshed = store.get_application(app_id, db_path=_DB_PATH)
        return _respond(handler, {"ok": True, "application": _public_app(refreshed or app)})
    except SkillHubConflictError as exc:
        store.update_application(
            app_id,
            fields={
                "status": PublishStatus.DRAFT,
                "version": "",
                "submitted_at": None,
            },
            db_path=_DB_PATH,
        )
        return _respond_bad(handler, f"上游名称/版本冲突: {exc}", 409)
    except SkillHubUpstreamError as exc:
        store.update_application(
            app_id,
            fields={
                "status": PublishStatus.DRAFT,
                "version": "",
                "submitted_at": None,
            },
            db_path=_DB_PATH,
        )
        error_msg = _friendly_upstream_error(str(exc))
        return _respond_bad(handler, error_msg, 502)
    finally:
        if tmp_zip is not None:
            zip_pack.safe_unlink(tmp_zip)


def _find_skill_safe(skill_name: str) -> tuple[Path | None, Any]:
    from integration.skills.local_skills import _find_skill_in_any_profile

    skill_dir, skill_md = _find_skill_in_any_profile(skill_name)
    return skill_dir, skill_md


def _platform() -> str:
    from integration.config import skill_publish_platform

    return skill_publish_platform()


def _friendly_upstream_error(exc_msg: str) -> str:
    """Convert upstream error messages to user-friendly Chinese messages."""
    msg_lower = exc_msg.lower()
    if "already exists" in msg_lower:
        # Extract skill name if present in the error message
        import re
        match = re.search(r"skill\s+name\s+'([^']+)'", exc_msg, re.IGNORECASE)
        if match:
            skill_name = match.group(1)
            return f"技能 '{skill_name}' 已在技能市场存在，请勿重复发布"
        return "该技能已在技能市场存在，请勿重复发布"
    return f"上游服务不可用: {exc_msg}"


# ── POST withdraw / DELETE ───────────────────────────────────────────────────


def _post_withdraw(handler, app_id: str, body: dict) -> bool:
    app = store.get_application(app_id, db_path=_DB_PATH)
    if not app:
        return _respond_bad(handler, "申请单不存在", 404)
    account = str(body.get("account") or "").strip()
    if not account:
        return _respond_bad(handler, "缺少用户标识 account", 400)
    if app.get("submitter_account") != account:
        return _respond_bad(handler, "无权操作该申请单", 403)
    if app.get("status") != PublishStatus.PENDING:
        return _respond_bad(handler, "仅审批中的申请可撤回", 409)
    upstream_application_id = app.get("upstream_application_id") or ""
    if not upstream_application_id:
        return _respond_bad(handler, "申请单缺少上游审批单 ID", 409)
    try:
        client.withdraw_approval(
            upstream_application_id,
            platform=_platform(),
            external_user_id=app.get("external_user_id") or account,
        )
    except SkillHubConflictError as exc:
        return _respond_bad(handler, f"上游拒绝撤回: {exc}", 409)
    except SkillHubUpstreamError as exc:
        error_msg = _friendly_upstream_error(str(exc))
        return _respond_bad(handler, error_msg, 502)
    store.update_application(
        app_id,
        fields={
            "status": PublishStatus.DRAFT,
            "upstream_status": "",
            "submitted_at": None,
        },
        db_path=_DB_PATH,
    )
    if app.get("application_type") == ApplicationType.UNPUBLISH:
        store.update_version_status(
            app["skill_name"], UPSTREAM_STATUS_LISTED, db_path=_DB_PATH
        )
    store.insert_audit_log(
        application_id=app_id,
        action=AuditAction.WITHDRAW,
        from_status=PublishStatus.PENDING,
        to_status=PublishStatus.DRAFT,
        operator=account,
        operator_role=OperatorRole.SUBMITTER,
        db_path=_DB_PATH,
    )
    refreshed = store.get_application(app_id, db_path=_DB_PATH)
    return _respond(handler, {"ok": True, "application": _public_app(refreshed or app)})


def _delete_application(handler, qs: dict[str, str], app_id: str) -> bool:
    app = store.get_application(app_id, db_path=_DB_PATH)
    if not app:
        return _respond_bad(handler, "申请单不存在", 404)
    if app.get("status") == PublishStatus.PENDING:
        store.soft_hide(app_id, db_path=_DB_PATH)
        return _respond(
            handler, {"ok": True, "id": app_id, "delete_type": "soft", "hidden": True}
        )
    store.hard_delete(app_id, db_path=_DB_PATH)
    return _respond(
        handler, {"ok": True, "id": app_id, "delete_type": "hard", "hidden": False}
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_json_field(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except (ValueError, TypeError):
        return default


def _list_item(app: dict, is_first: bool) -> dict:
    keys = (
        "id", "skill_name", "display_name", "description", "display_description",
        "category", "tags", "application_type", "reason", "version", "status",
        "submitter_account", "upstream_name", "upstream_skill_id",
        "upstream_application_id", "upstream_status", "audit_comment",
        "created_at", "updated_at", "submitted_at", "audited_at", "hidden",
    )
    result = {k: app.get(k) for k in keys}
    result["is_first"] = is_first
    return result


def _public_app(app: dict) -> dict:
    keys = (
        "id", "skill_name", "display_name", "description", "display_description",
        "category", "tags", "detail_json", "skill_md_content", "application_type",
        "reason", "version", "status", "submitter_account", "applicant_name",
        "applicant_org", "applicant_title", "platform", "upstream_name",
        "upstream_skill_id", "upstream_application_id", "upstream_status",
        "audit_comment", "created_at", "updated_at", "submitted_at", "audited_at",
        "hidden",
    )
    return {k: app.get(k) for k in keys}
