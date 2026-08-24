"""Tests for skill_publish HTTP handlers (store DB + upstream client mocked)."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.skill_publish import handlers, store
from integration.skill_publish.client import (
    SkillHubConflictError,
    SkillHubUpstreamError,
)
from integration.skill_publish.constants import PublishStatus


def _payload(handler):
    raw = handler.wfile.write.call_args.args[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "session_manifest.db"


@pytest.fixture(autouse=True)
def enabled():
    with patch.object(handlers, "skill_publish_enabled", return_value=True):
        yield


@pytest.fixture
def db_path_patch(db, monkeypatch):
    monkeypatch.setattr(handlers, "_DB_PATH", db)
    monkeypatch.setattr(
        handlers, "_current_user", lambda: ("acct-a", "uuid-a")
    )
    return db


def _patch_local_skill(monkeypatch, tmp_path, *, md_content=None):
    """Point the lazy local-skill imports at a fake on-disk skill."""
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        md_content or "---\nname: demo-skill\ndescription: A demo skill\n---\nbody",
        encoding="utf-8",
    )
    (skill_dir / ".detail.json").write_text(
        json.dumps({
            "display_name": "Demo Skill",
            "display_description": "demo desc",
            "category": "tools",
            "tags": ["a", "b"],
            "detail_json": {"steps": 1},
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "integration.skills.paths.shared_skills_dir",
        lambda: tmp_path / "skills",
    )
    return skill_dir


def _make_app(db, **overrides):
    fields = {
        "skill_name": "demo-skill",
        "display_name": "Demo Skill",
        "application_type": "publish",
        "status": PublishStatus.DRAFT,
        "submitter_account": "acct-a",
        "submitter_uuid": "uuid-a",
        "external_user_id": "acct-a",
    }
    fields.update(overrides)
    return store.create_application(fields=fields, db_path=db)


# ── disabled gate ─────────────────────────────────────────────────────────────


def test_handlers_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=a")
    with patch.object(handlers, "skill_publish_enabled", return_value=False):
        assert handlers.try_handle_get(handler, parsed) is False
        assert handlers.try_handle_post(handler, parsed, {}) is False
        assert handlers.try_handle_delete(handler, parsed) is False


# ── GET list ──────────────────────────────────────────────────────────────────


def test_get_list_requires_account(db_path_patch):
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    assert handlers.try_handle_get(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 400


def test_get_list_returns_items_and_stats(db_path_patch):
    _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["total"] == 1
    assert payload["items"][0]["skill_name"] == "demo-skill"
    assert payload["stats"]["by_status"][PublishStatus.DRAFT] == 1


def test_get_list_omits_detail_json_and_md_content(db_path_patch):
    _make_app(
        db_path_patch,
        detail_json=json.dumps({"steps": 1}),
        skill_md_content="---\nname: x\n---\n",
    )
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    item = payload["items"][0]
    assert "detail_json" not in item
    assert "skill_md_content" not in item


def test_get_list_is_first_true_for_first_publish(db_path_patch):
    _make_app(
        db_path_patch, application_type="publish", version="1.0.0",
        status=PublishStatus.PENDING,
    )
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["items"][0]["is_first"] is True


def test_get_list_is_first_false_for_update_publish(db_path_patch):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a", "upstream_status": "2"},
        db_path=db_path_patch,
    )
    _make_app(
        db_path_patch, application_type="publish", version="1.0.1",
        status=PublishStatus.DRAFT,
    )
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["items"][0]["is_first"] is False


def test_get_list_is_first_false_for_unpublish(db_path_patch):
    _make_app(
        db_path_patch, application_type="unpublish", version="1.0.0",
        status=PublishStatus.DRAFT,
    )
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["items"][0]["is_first"] is False


# ── POST create ───────────────────────────────────────────────────────────────


def test_post_create_publish_backfills_snapshot(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a", "uuid": "uuid-a"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    payload = _payload(handler)
    app = payload["application"]
    assert app["status"] == PublishStatus.DRAFT
    assert app["display_name"] == "Demo Skill"
    assert app["description"] == "A demo skill"
    assert app["skill_md_content"].startswith("---")


def test_post_create_publish_local_missing_404(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "ghost", "account": "acct-a"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 404


def test_post_create_unpublish_requires_reason(db_path_patch):
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a",
            "application_type": "unpublish"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 400


def _publish_version(db, account="acct-a", upstream_status="2", *, application_id="",
                     display_name="Demo Skill"):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": account, "upstream_status": upstream_status,
                "application_id": application_id, "display_name": display_name},
        db_path=db,
    )


def test_post_create_unpublish_not_owner_403(db_path_patch):
    _publish_version(db_path_patch, account="acct-a")
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-b",
            "application_type": "unpublish", "reason": "bye"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 403


def test_post_create_unpublish_no_local_version_403(db_path_patch):
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a",
            "application_type": "unpublish", "reason": "bye"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 403


def test_post_create_unpublish_not_listed_409(db_path_patch):
    _publish_version(db_path_patch, upstream_status="3")
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a",
            "application_type": "unpublish", "reason": "bye"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 409


def test_post_create_unpublish_success(db_path_patch):
    src_app = store.create_application(
        fields={
            "skill_name": "demo-skill",
            "display_name": "Demo Skill",
            "description": "A demo skill",
            "display_description": "demo desc",
            "category": "tools",
            "tags": json.dumps(["a", "b"], ensure_ascii=False),
            "detail_json": json.dumps({"steps": 1}, ensure_ascii=False),
            "skill_md_content": "---\nname: demo-skill\n---\nbody",
            "application_type": "publish",
            "status": PublishStatus.APPROVED,
            "submitter_account": "acct-a",
            "submitter_uuid": "uuid-a",
            "external_user_id": "acct-a",
            "version": "1.0.0",
        },
        db_path=db_path_patch,
    )
    _publish_version(db_path_patch, application_id=src_app["id"])
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a", "uuid": "uuid-a",
            "application_type": "unpublish", "reason": "cleanup"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    app = _payload(handler)["application"]
    assert app["application_type"] == "unpublish"
    assert app["version"] == "1.0.0"
    assert app["display_name"] == "Demo Skill"
    assert app["description"] == "A demo skill"
    assert app["display_description"] == "demo desc"
    assert app["category"] == "tools"
    assert app["tags"] == json.dumps(["a", "b"], ensure_ascii=False)
    assert app["detail_json"] == json.dumps({"steps": 1}, ensure_ascii=False)
    assert app["skill_md_content"].startswith("---")


def test_post_create_active_exists_409(db_path_patch):
    _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications")
    body = {"skill_name": "demo-skill", "account": "acct-a"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 409


# ── POST update (draft only) ─────────────────────────────────────────────────


@pytest.fixture
def db_update(db, monkeypatch):
    monkeypatch.setattr(handlers, "_DB_PATH", db)
    return db


def _draft(db, **extra):
    fields = {
        "skill_name": "demo-skill",
        "application_type": "publish",
        "reason": "",
        "status": PublishStatus.DRAFT,
        "submitter_account": "acct-a",
    }
    fields.update(extra)
    return store.create_application(fields=fields, db_path=db)


def _call_update(handler, app_id, body):
    parsed = urlparse(f"/api/skillhub/publish/applications/{app_id}")
    return handlers.try_handle_post(handler, parsed, body)


def test_post_update_draft_fields(db_update):
    app = _draft(db_update, reason="old", category="tools")
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"reason": "new-reason",
                                             "category": "productivity"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["ok"] is True
    assert payload["application"]["reason"] == "new-reason"
    assert payload["application"]["category"] == "productivity"
    assert payload["application"]["application_type"] == "publish"


def test_post_update_not_found_404(db_update):
    handler = MagicMock()
    assert _call_update(handler, "skp-nope", {"reason": "x"}) is True
    assert handler.send_response.call_args.args[0] == 404


def test_post_update_not_draft_409(db_update):
    app = _draft(db_update, status=PublishStatus.PENDING)
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"reason": "x"}) is True
    assert handler.send_response.call_args.args[0] == 409


def test_post_update_rejected_fields(db_update):
    app = _draft(db_update, status=PublishStatus.REJECTED, reason="old",
                 audit_comment="理由不充分")
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"reason": "更充分的理由"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["application"]["reason"] == "更充分的理由"
    assert payload["application"]["status"] == PublishStatus.REJECTED
    assert payload["application"]["audit_comment"] == "理由不充分"


def test_post_update_rejected_switch_to_unpublish(db_update):
    _publish_version(db_update, account="acct-a")
    app = _draft(db_update, status=PublishStatus.REJECTED)
    handler = MagicMock()
    assert _call_update(handler, app["id"],
                        {"application_type": "unpublish", "reason": "cleanup"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["application"]["application_type"] == "unpublish"
    assert payload["application"]["version"] == "1.0.0"


def test_post_update_invalid_type_400(db_update):
    app = _draft(db_update)
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"application_type": "bogus"}) is True
    assert handler.send_response.call_args.args[0] == 400


def test_post_update_no_fields_400(db_update):
    app = _draft(db_update)
    handler = MagicMock()
    assert _call_update(handler, app["id"], {}) is True
    assert handler.send_response.call_args.args[0] == 400


def test_post_update_switch_to_unpublish(db_update):
    _publish_version(db_update, account="acct-a")
    app = _draft(db_update)
    handler = MagicMock()
    assert _call_update(handler, app["id"],
                        {"application_type": "unpublish", "reason": "cleanup"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["application"]["application_type"] == "unpublish"
    assert payload["application"]["version"] == "1.0.0"
    assert payload["application"]["reason"] == "cleanup"


def test_post_update_switch_to_unpublish_requires_reason_400(db_update):
    _publish_version(db_update, account="acct-a")
    app = _draft(db_update)
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"application_type": "unpublish"}) is True
    assert handler.send_response.call_args.args[0] == 400


def test_post_update_switch_conflict_409(db_update):
    _publish_version(db_update, account="acct-a")
    app = _draft(db_update)
    _draft(db_update, application_type="unpublish", reason="bye")
    handler = MagicMock()
    assert _call_update(handler, app["id"],
                        {"application_type": "unpublish", "reason": "cleanup"}) is True
    assert handler.send_response.call_args.args[0] == 409


def test_post_update_switch_to_unpublish_not_listed_409(db_update):
    _publish_version(db_update, account="acct-a", upstream_status="3")
    app = _draft(db_update)
    handler = MagicMock()
    assert _call_update(handler, app["id"],
                        {"application_type": "unpublish", "reason": "cleanup"}) is True
    assert handler.send_response.call_args.args[0] == 409


def test_post_update_switch_to_publish_clears_reason(db_update):
    app = _draft(db_update, application_type="unpublish", reason="old reason",
                 version="1.0.0")
    handler = MagicMock()
    assert _call_update(handler, app["id"], {"application_type": "publish"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["application"]["application_type"] == "publish"
    assert payload["application"]["reason"] == ""
    assert payload["application"]["version"] == ""


def test_post_update_switch_to_publish_keeps_explicit_reason(db_update):
    app = _draft(db_update, application_type="unpublish", reason="old reason")
    handler = MagicMock()
    assert _call_update(handler, app["id"],
                        {"application_type": "publish", "reason": "updated"}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _payload(handler)
    assert payload["application"]["reason"] == "updated"


# ── GET detail ────────────────────────────────────────────────────────────────


def test_get_detail_owner_with_audit_log(db_path_patch):
    app = _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["application"]["id"] == app["id"]
    assert payload["audit_log"] == []


def test_get_detail_wrong_account_403(db_path_patch):
    app = _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}?account=acct-b")
    assert handlers.try_handle_get(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 403


def test_get_detail_missing_404(db_path_patch):
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/applications/skp-missing?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 404


# ── POST submit ───────────────────────────────────────────────────────────────


def _submit(handler_db, app_id, account="acct-a", **body_extra):
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app_id}/submit")
    body = {"account": account, **body_extra}
    return handler, handlers.try_handle_post(handler, parsed, body)


def test_submit_success_publish(db_path_patch, monkeypatch, tmp_path):
    skill_dir = _patch_local_skill(monkeypatch, tmp_path)
    app = _make_app(db_path_patch)
    unlink_calls = []
    with patch.object(
        handlers.client, "upload_skill",
        return_value={
            "applicationId": "up-9", "applyTime": "2026-08-17 10:00:00",
            "name": "demo-skill", "skillId": "sk-1", "status": 1,
        },
    ) as mock_upload:
        with patch.object(
            handlers.zip_pack, "create_temp_zip_path",
            return_value=tmp_path / "t.zip",
        ):
            with patch.object(
                handlers.zip_pack, "safe_unlink",
                side_effect=lambda p: unlink_calls.append(p),
            ):
                with patch.object(
                    handlers.zip_pack, "build_skill_zip", return_value=2
                ) as mock_zip:
                    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    payload = _payload(handler)
    refreshed = payload["application"]
    assert refreshed["status"] == PublishStatus.PENDING
    assert refreshed["version"] == "1.0.0"
    assert refreshed["upstream_application_id"] == "up-9"

    assert mock_upload.call_args.kwargs["version"] == "1.0.0"
    assert mock_upload.call_args.args[1] == "demo-skill-1.0.0.zip"
    mock_zip.assert_called_once()
    assert unlink_calls == [tmp_path / "t.zip"]

    logs = store.list_audit_log(app["id"], db_path=db_path_patch)
    assert [l["action"] for l in logs] == ["submit"]


def test_submit_rejected_app_records_re_submit(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    app = _make_app(db_path_patch, status=PublishStatus.REJECTED)
    with patch.object(
        handlers.client, "upload_skill",
        return_value={"applicationId": "up-9", "applyTime": "2026-08-17 10:00:00"},
    ):
        with patch.object(handlers.zip_pack, "create_temp_zip_path",
                          return_value=tmp_path / "t.zip"):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(handlers.zip_pack, "build_skill_zip", return_value=1):
                    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    logs = store.list_audit_log(app["id"], db_path=db_path_patch)
    assert [l["action"] for l in logs] == ["re_submit"]


def test_submit_conflict_rolls_back_to_draft(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    app = _make_app(db_path_patch)
    with patch.object(
        handlers.client, "upload_skill",
        side_effect=SkillHubConflictError("name conflict"),
    ):
        with patch.object(handlers.zip_pack, "create_temp_zip_path",
                          return_value=tmp_path / "t.zip"):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(handlers.zip_pack, "build_skill_zip", return_value=1):
                    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 409
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["status"] == PublishStatus.DRAFT
    assert refreshed["version"] == ""
    assert refreshed["submitted_at"] is None


def test_submit_upstream_error_rolls_back_and_502(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    app = _make_app(db_path_patch)
    with patch.object(
        handlers.client, "upload_skill",
        side_effect=SkillHubUpstreamError("down", status_code=503),
    ):
        with patch.object(handlers.zip_pack, "create_temp_zip_path",
                          return_value=tmp_path / "t.zip"):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(handlers.zip_pack, "build_skill_zip", return_value=1):
                    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 502
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["status"] == PublishStatus.DRAFT


def test_submit_from_pending_409(db_path_patch):
    app = _make_app(db_path_patch, status=PublishStatus.PENDING)
    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 409


def test_submit_version_increments(db_path_patch, monkeypatch, tmp_path):
    _patch_local_skill(monkeypatch, tmp_path)
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.3",
                "submitter_account": "acct-a"},
        db_path=db_path_patch,
    )
    app = _make_app(db_path_patch)
    with patch.object(
        handlers.client, "upload_skill",
        return_value={"applicationId": "up-9", "applyTime": "2026-08-17 10:00:00"},
    ) as mock_upload:
        with patch.object(handlers.zip_pack, "create_temp_zip_path",
                          return_value=tmp_path / "t.zip"):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(handlers.zip_pack, "build_skill_zip", return_value=1):
                    handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    assert mock_upload.call_args.kwargs["version"] == "1.0.4"


def test_submit_unpublish_keeps_version(db_path_patch):
    _publish_version(db_path_patch)
    app = _make_app(
        db_path_patch, application_type="unpublish", version="1.0.0",
        status=PublishStatus.DRAFT,
    )
    with patch.object(
        handlers.client, "unpublish_skill",
        return_value={"applicationId": "up-5", "status": 4},
    ) as mock_unpub:
        handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    assert mock_unpub.call_args.kwargs["reason"] == ""
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["status"] == PublishStatus.PENDING
    assert refreshed["version"] == "1.0.0"
    assert refreshed["upstream_status"] == "4"
    latest = store.get_latest_version("demo-skill", db_path=db_path_patch)
    assert latest["upstream_status"] == "4"


def test_submit_unpublish_defaults_pending_status(db_path_patch):
    """If upstream response omits status, default to pending-unpublish (4)."""
    _publish_version(db_path_patch)
    app = _make_app(
        db_path_patch, application_type="unpublish", version="1.0.0",
        status=PublishStatus.DRAFT,
    )
    with patch.object(
        handlers.client, "unpublish_skill",
        return_value={"applicationId": "up-6", "applyTime": "2026-08-17 10:00:00"},
    ):
        handler, handled = _submit(db_path_patch, app["id"])
    assert handled is True
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["upstream_status"] == "4"
    latest = store.get_latest_version("demo-skill", db_path=db_path_patch)
    assert latest["upstream_status"] == "4"


# ── POST withdraw ─────────────────────────────────────────────────────────────


def test_withdraw_success(db_path_patch):
    app = _make_app(
        db_path_patch, status=PublishStatus.PENDING,
        upstream_application_id="up-1",
    )
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}/withdraw")
    body = {"account": "acct-a"}
    with patch.object(handlers.client, "withdraw_approval", return_value={}):
        assert handlers.try_handle_post(handler, parsed, body) is True
    payload = _payload(handler)
    assert payload["application"]["status"] == PublishStatus.DRAFT
    assert payload["application"]["upstream_status"] == ""
    logs = store.list_audit_log(app["id"], db_path=db_path_patch)
    assert [l["action"] for l in logs] == ["withdraw"]


def test_withdraw_unpublish_restores_version_status(db_path_patch):
    _publish_version(db_path_patch, upstream_status="4")
    app = _make_app(
        db_path_patch,
        application_type="unpublish",
        status=PublishStatus.PENDING,
        upstream_application_id="up-ub-1",
        version="1.0.0",
    )
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}/withdraw")
    body = {"account": "acct-a"}
    with patch.object(handlers.client, "withdraw_approval", return_value={}):
        assert handlers.try_handle_post(handler, parsed, body) is True
    latest = store.get_latest_version("demo-skill", db_path=db_path_patch)
    assert latest["upstream_status"] == "2"


def test_withdraw_publish_does_not_touch_versions(db_path_patch):
    app = _make_app(
        db_path_patch, status=PublishStatus.PENDING,
        upstream_application_id="up-1",
    )
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}/withdraw")
    body = {"account": "acct-a"}
    with patch.object(handlers.client, "withdraw_approval", return_value={}):
        assert handlers.try_handle_post(handler, parsed, body) is True
    # No published version exists for publish-only app; get_latest_version returns None
    assert store.get_latest_version("demo-skill", db_path=db_path_patch) is None


def test_withdraw_not_pending_409(db_path_patch):
    app = _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}/withdraw")
    body = {"account": "acct-a"}
    assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 409


def test_withdraw_upstream_error_keeps_state(db_path_patch):
    app = _make_app(
        db_path_patch, status=PublishStatus.PENDING,
        upstream_application_id="up-1",
    )
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}/withdraw")
    body = {"account": "acct-a"}
    with patch.object(
        handlers.client, "withdraw_approval",
        side_effect=SkillHubUpstreamError("down"),
    ):
        assert handlers.try_handle_post(handler, parsed, body) is True
    assert handler.send_response.call_args.args[0] == 502
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["status"] == PublishStatus.PENDING


# ── DELETE ────────────────────────────────────────────────────────────────────


def test_delete_pending_is_soft(db_path_patch):
    app = _make_app(db_path_patch, status=PublishStatus.PENDING)
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}?account=acct-a")
    assert handlers.try_handle_delete(handler, parsed) is True
    payload = _payload(handler)
    assert payload["delete_type"] == "soft"
    assert payload["hidden"] is True
    refreshed = store.get_application(app["id"], db_path=db_path_patch)
    assert refreshed["hidden"] == 1


def test_delete_draft_is_hard_with_audit_cascade(db_path_patch):
    app = _make_app(db_path_patch)
    store.insert_audit_log(
        application_id=app["id"], action="submit", from_status="draft",
        to_status="pending", operator="acct-a", operator_role="submitter",
        db_path=db_path_patch,
    )
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}?account=acct-a")
    assert handlers.try_handle_delete(handler, parsed) is True
    payload = _payload(handler)
    assert payload["delete_type"] == "hard"
    assert store.get_application(app["id"], db_path=db_path_patch) is None


def test_delete_wrong_account_403(db_path_patch):
    app = _make_app(db_path_patch)
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app['id']}?account=acct-b")
    assert handlers.try_handle_delete(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 403


# ── versions endpoints ────────────────────────────────────────────────────────


def test_get_application_versions(db_path_patch):
    app = _make_app(db_path_patch)
    store.update_application(
        app["id"], fields={"submitted_at": 1_000_000.0, "version": "1.0.0"},
        db_path=db_path_patch,
    )
    handler = MagicMock()
    parsed = urlparse(
        f"/api/skillhub/publish/applications/{app['id']}/versions?skill_name=demo-skill"
    )
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["versions"][0]["version"] == "1.0.0"
    assert payload["versions"][0]["application_id"] == app["id"]


def test_get_skill_versions_not_exists(db_path_patch):
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/skill-versions?skill_name=new")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["exists"] is False
    assert payload["next_version"] == "1.0.0"


def test_get_skill_versions_exists(db_path_patch):
    store.insert_version_and_demote(
        fields={"skill_name": "demo-skill", "version": "1.0.0",
                "submitter_account": "acct-a"},
        db_path=db_path_patch,
    )
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/skill-versions?skill_name=demo-skill")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["exists"] is True
    assert payload["latest_version"] == "1.0.0"
    assert payload["next_version"] == "1.0.1"


def test_get_my_published_skills(db_path_patch):
    _publish_version(db_path_patch)
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/my-published-skills?account=acct-a")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["skill_name"] == "demo-skill"
    assert payload["items"][0]["latest_version"] == "1.0.0"


# ── routing ───────────────────────────────────────────────────────────────────


def test_unknown_path_returns_false(db_path_patch):
    handler = MagicMock()
    assert handlers.try_handle_get(
        handler, urlparse("/api/skillhub/publish/unknown")
    ) is False
    assert handlers.try_handle_post(
        handler, urlparse("/api/skillhub/publish/unknown"), {}
    ) is False
    assert handlers.try_handle_delete(
        handler, urlparse("/api/skillhub/publish/unknown")
    ) is False


# ── _friendly_upstream_error ─────────────────────────────────────────────────


def test_friendly_upstream_error_already_exists_with_skill_name():
    """Test that 'already exists' errors with skill name are converted to friendly Chinese message."""
    error_msg = "skillhub 400: Skill name 'summarize' already exists"
    result = handlers._friendly_upstream_error(error_msg)
    assert result == "技能 'summarize' 已在技能市场存在，请勿重复发布"


def test_friendly_upstream_error_already_exists_without_skill_name():
    """Test that 'already exists' errors without skill name are converted to generic message."""
    error_msg = "skillhub 400: Already exists"
    result = handlers._friendly_upstream_error(error_msg)
    assert result == "该技能已在技能市场存在，请勿重复发布"


def test_friendly_upstream_error_other_errors():
    """Test that other errors are wrapped with '上游服务不可用' prefix."""
    error_msg = "skillhub 500: Internal server error"
    result = handlers._friendly_upstream_error(error_msg)
    assert result == "上游服务不可用: skillhub 500: Internal server error"


def test_friendly_upstream_error_already_exists_case_insensitive():
    """Test that 'already exists' matching is case-insensitive."""
    error_msg = "skillhub 400: Skill Name 'test-skill' Already Exists"
    result = handlers._friendly_upstream_error(error_msg)
    assert result == "技能 'test-skill' 已在技能市场存在，请勿重复发布"
