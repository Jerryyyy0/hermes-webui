"""Tests for pre-publish skill package validation (validate.py + endpoints)."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.skill_publish import handlers, store, validate
from integration.skill_publish.constants import PublishStatus


VALID_MD = "---\nname: demo-skill\ndescription: A demo skill\n---\nbody"


def _payload(handler):
    raw = handler.wfile.write.call_args.args[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


@pytest.fixture(autouse=True)
def enabled():
    with patch.object(handlers, "skill_publish_enabled", return_value=True):
        yield


def _make_skill(monkeypatch, tmp_path, files: dict[str, str]) -> "object":
    """Create skills/demo-skill with the given relative-path -> content files."""
    skills = tmp_path / "skills"
    skill_dir = skills / "demo-skill"
    for rel, content in files.items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # local_skills binds shared_skills_dir via from-import; patch its binding.
    monkeypatch.setattr(
        "integration.skills.local_skills.shared_skills_dir", lambda: skills
    )
    return skill_dir


def _codes(issues: list[dict]) -> list[str]:
    return [i["code"] for i in issues]


# ── validate_skill_package ─────────────────────────────────────────────────────


def test_valid_package_has_no_issues(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    assert validate.validate_skill_package("demo-skill", skill_dir) == []


def test_missing_skill_md(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {"README.md": "hi"})
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["SKILL_MD_REQUIRED"]


def test_dotfile_skill_md_not_counted(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "README.md": "hi",
        ".hidden/SKILL.md": VALID_MD,  # excluded from the package
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["SKILL_MD_REQUIRED"]


def test_multiple_skill_md_in_subdir(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "sub/skill.md": VALID_MD,
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["MULTIPLE_SKILL_MD"]
    assert issues[0]["files"] == ["SKILL.md", "sub/skill.md"]


def test_multiple_skill_md_case_variants(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "other/Skill.MD": VALID_MD,
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["MULTIPLE_SKILL_MD"]
    assert set(issues[0]["files"]) == {"SKILL.md", "other/Skill.MD"}


def test_single_nested_skill_md_not_at_root(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {"sub/SKILL.md": VALID_MD})
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["SKILL_MD_NOT_ROOT"]
    assert issues[0]["file"] == "sub/SKILL.md"


def test_invalid_skill_md_no_frontmatter(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {"SKILL.md": "# heading only"})
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["INVALID_SKILL_MD"]


def test_invalid_skill_md_missing_description(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": "---\nname: demo-skill\n---\nbody"
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["INVALID_SKILL_MD"]
    assert "description" in issues[0]["message_zh"]


def test_name_charset_chinese_rejected(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": "---\nname: 技能-demo\ndescription: ok\n---\nbody"
    })
    issues = validate.validate_skill_package("技能-demo", skill_dir)
    assert _codes(issues) == ["NAME_CHARSET"]


def test_name_mismatch_is_warning(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": "---\nname: other-name\ndescription: ok\n---\nbody"
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["NAME_MISMATCH"]
    assert issues[0]["severity"] == "warning"


def test_junk_files_are_warning(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "__pycache__/run.pyc": "bytecode",
    })
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["JUNK_FILE"]
    assert issues[0]["severity"] == "warning"
    assert issues[0]["files"] == ["__pycache__/run.pyc"]


def test_package_too_large(monkeypatch, tmp_path):
    skill_dir = _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "big.bin": "x" * 100,
    })
    monkeypatch.setattr(validate, "_MAX_PACKAGE_BYTES", 10)
    issues = validate.validate_skill_package("demo-skill", skill_dir)
    assert _codes(issues) == ["PACKAGE_TOO_LARGE"]
    assert issues[0]["severity"] == "error"


# ── resolve_skill_dir ──────────────────────────────────────────────────────────


def test_resolve_skill_dir_exact(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    resolved = validate.resolve_skill_dir("demo-skill")
    assert resolved is not None
    assert resolved.name == "demo-skill"


def test_resolve_skill_dir_fallback_without_skill_md(monkeypatch, tmp_path):
    """Dir exists but SKILL.md is missing: resolve via fallback, not 404."""
    skill_dir = _make_skill(monkeypatch, tmp_path, {"README.md": "hi"})
    assert validate.resolve_skill_dir("demo-skill") == skill_dir


def test_resolve_skill_dir_missing_returns_none(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    assert validate.resolve_skill_dir("ghost-skill") is None


# ── GET /api/skillhub/publish/validate ────────────────────────────────────────


def test_get_validate_requires_skill_name():
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate")
    assert handlers.try_handle_get(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 400


def test_get_validate_unknown_skill_404(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate?skill_name=ghost")
    assert handlers.try_handle_get(handler, parsed) is True
    assert handler.send_response.call_args.args[0] == 404


def test_get_validate_valid_skill(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate?skill_name=demo-skill")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["valid"] is True
    assert payload["skill_name"] == "demo-skill"
    assert payload["issues"] == []


def test_get_validate_multiple_skill_md(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "sub/skill.md": VALID_MD,
    })
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate?skill_name=demo-skill")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["valid"] is False
    issue = payload["issues"][0]
    assert issue["code"] == "MULTIPLE_SKILL_MD"
    assert issue["severity"] == "error"
    assert issue["files"] == ["SKILL.md", "sub/skill.md"]


def test_get_validate_missing_skill_md_not_404(monkeypatch, tmp_path):
    """SKILL.md missing: report the issue, not '本地技能不存在'."""
    _make_skill(monkeypatch, tmp_path, {"README.md": "hi"})
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate?skill_name=demo-skill")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["valid"] is False
    assert payload["issues"][0]["code"] == "SKILL_MD_REQUIRED"


def test_get_validate_warning_does_not_block(monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "__pycache__/run.pyc": "bytecode",
    })
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/publish/validate?skill_name=demo-skill")
    assert handlers.try_handle_get(handler, parsed) is True
    payload = _payload(handler)
    assert payload["valid"] is True
    assert payload["issues"][0]["code"] == "JUNK_FILE"


# ── _post_submit interception ────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "session_manifest.db"
    monkeypatch.setattr(handlers, "_DB_PATH", db_path)
    return db_path


def _make_app(db, **overrides):
    fields = {
        "skill_name": "demo-skill",
        "application_type": "publish",
        "status": PublishStatus.DRAFT,
        "submitter_account": "acct-a",
        "submitter_uuid": "uuid-a",
        "external_user_id": "acct-a",
    }
    fields.update(overrides)
    return store.create_application(fields=fields, db_path=db)


def _submit(db, app_id, account="acct-a"):
    handler = MagicMock()
    parsed = urlparse(f"/api/skillhub/publish/applications/{app_id}/submit")
    handled = handlers.try_handle_post(handler, parsed, {"account": account})
    return handler, handled


def test_submit_blocked_by_multiple_skill_md(db, monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "sub/skill.md": VALID_MD,
    })
    app = _make_app(db)
    with patch.object(handlers.client, "upload_skill") as mock_upload:
        with patch.object(handlers.zip_pack, "create_temp_zip_path") as mock_zip:
            handler, handled = _submit(db, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 400
    payload = _payload(handler)
    assert payload["issues"][0]["code"] == "MULTIPLE_SKILL_MD"
    mock_upload.assert_not_called()
    mock_zip.assert_not_called()
    # Interception happens before any state change: app stays DRAFT.
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.DRAFT


def test_submit_blocked_by_invalid_name_charset(db, monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": "---\nname: 技能demo\ndescription: ok\n---\nbody"
    })
    app = _make_app(db)
    with patch.object(handlers.client, "upload_skill") as mock_upload:
        handler, handled = _submit(db, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 400
    payload = _payload(handler)
    assert payload["issues"][0]["code"] == "NAME_CHARSET"
    mock_upload.assert_not_called()


def test_submit_blocked_when_skill_md_missing(db, monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"README.md": "hi"})
    app = _make_app(db)
    with patch.object(handlers.client, "upload_skill") as mock_upload:
        handler, handled = _submit(db, app["id"])
    assert handled is True
    assert handler.send_response.call_args.args[0] == 400
    payload = _payload(handler)
    assert payload["issues"][0]["code"] == "SKILL_MD_REQUIRED"
    mock_upload.assert_not_called()
    assert store.get_application(app["id"], db_path=db)["status"] == PublishStatus.DRAFT


def test_submit_success_with_valid_skill(db, monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {"SKILL.md": VALID_MD})
    app = _make_app(db)
    with patch.object(
        handlers.client, "upload_skill",
        return_value={
            "applicationId": "up-9", "applyTime": "2026-09-11 10:00:00",
            "name": "demo-skill", "skillId": "sk-1", "status": 1,
        },
    ) as mock_upload:
        with patch.object(
            handlers.zip_pack, "create_temp_zip_path",
            return_value=tmp_path / "t.zip",
        ):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(
                    handlers.zip_pack, "build_skill_zip", return_value=1
                ) as mock_zip:
                    handler, handled = _submit(db, app["id"])
    assert handled is True
    payload = _payload(handler)
    assert payload["application"]["status"] == PublishStatus.PENDING
    assert payload["application"]["version"] == "1.0.0"
    mock_upload.assert_called_once()
    mock_zip.assert_called_once()
    assert mock_zip.call_args.args[0].name == "demo-skill"


def test_submit_warning_does_not_block(db, monkeypatch, tmp_path):
    _make_skill(monkeypatch, tmp_path, {
        "SKILL.md": VALID_MD,
        "__pycache__/run.pyc": "bytecode",
    })
    app = _make_app(db)
    with patch.object(
        handlers.client, "upload_skill",
        return_value={"applicationId": "up-9", "applyTime": "2026-09-11 10:00:00"},
    ) as mock_upload:
        with patch.object(
            handlers.zip_pack, "create_temp_zip_path",
            return_value=tmp_path / "t.zip",
        ):
            with patch.object(handlers.zip_pack, "safe_unlink", lambda p: None):
                with patch.object(handlers.zip_pack, "build_skill_zip", return_value=1):
                    handler, handled = _submit(db, app["id"])
    assert handled is True
    assert mock_upload.call_count == 1
    refreshed = store.get_application(app["id"], db_path=db)
    assert refreshed["status"] == PublishStatus.PENDING
