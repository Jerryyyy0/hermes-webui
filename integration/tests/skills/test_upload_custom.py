"""Custom skill upload (local only, no upstream)."""

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from integration.skills import local_skills
from integration.skills.handlers import handle_skillhub_upload
from integration.skills.utils import extract_zip_and_flatten


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_normalize_and_resolve_dir_name():
    assert local_skills.normalize_dir_name("My Skill") == "my-skill"
    name, err = local_skills.resolve_dir_name("Custom", "ignored.zip")
    assert err is None and name == "custom"
    name, err = local_skills.resolve_dir_name("", "Report.md")
    assert err is None and name == "report"
    _, err = local_skills.resolve_dir_name("", None)
    assert err and err["status"] == 400


def test_upload_json_writes_skill_md(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        request_name="my-skill",
        content="---\nname: my-skill\ndescription: test\n---\n# Hi",
    )
    assert result.get("ok") is True
    assert result["skill_count"] == 1
    assert result["file_count"] == 1
    assert result["skills"][0]["dir_name"] == "my-skill"
    assert result["skills"][0]["name"] == "my-skill"
    assert (skills_dir / "my-skill" / "SKILL.md").is_file()


def test_upload_zip_flattens_prefix(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    z = _zip_bytes(
        {
            "bundle/SKILL.md": "---\nname: bundled\n---\n",
            "bundle/scripts/run.py": "print(1)\n",
        }
    )
    result = local_skills.upload_custom_skill(
        request_name="my-tool",
        zip_bytes=z,
        filename="data-tools.zip",
    )
    assert result["ok"] is True
    assert result["skill_count"] == 1
    assert result["file_count"] == 2
    assert result["skills"][0]["dir_name"] == "bundled"
    assert result["skills"][0]["name"] == "bundled"
    assert (skills_dir / "bundled" / "SKILL.md").is_file()
    assert (skills_dir / "bundled" / "scripts" / "run.py").is_file()


def test_upload_with_category_writes_nested_path(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        request_name="apple-notes",
        category="apple",
        content="---\nname: apple-notes\ndescription: notes app\n---\n# Hi\n",
    )
    assert result.get("ok") is True
    assert result["skill_count"] == 1
    assert result["file_count"] == 1
    assert result["skills"][0]["category"] == "apple"
    assert result["skills"][0]["dir_name"] == "apple/apple-notes"
    skill_dir = skills_dir / "apple" / "apple-notes"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / ".category").read_text(encoding="utf-8") == "apple"


def test_upload_conflict_409(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "exists"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: exists\n---\n", encoding="utf-8")
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        request_name="exists",
        content="---\nname: exists\ndescription: x\n---\n",
    )
    assert result["status"] == 409


def test_upload_rejects_invalid_frontmatter(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        request_name="bad",
        content="# missing frontmatter\n",
    )
    assert result["status"] == 400
    assert not (skills_dir / "bad").exists()


def test_upload_rejects_missing_description(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        request_name="nodesc",
        content="---\nname: nodesc\n---\n# Hi\n",
    )
    assert result["status"] == 400
    assert not (skills_dir / "nodesc").exists()


def test_upload_zip_multi_skills(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    z = _zip_bytes(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
        }
    )
    result = local_skills.upload_custom_skill(
        category="tools",
        zip_bytes=z,
        filename="bundle.zip",
    )
    assert result["ok"] is True
    assert result["skill_count"] == 2
    assert result["file_count"] == 2
    dirs = {s["dir_name"] for s in result["skills"]}
    assert dirs == {"tools/alpha", "tools/beta"}


def test_upload_zip_batch_rollback_on_conflict(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    existing = skills_dir / "tools" / "alpha"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\n", encoding="utf-8")
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    z = _zip_bytes(
        {
            "alpha/SKILL.md": "---\nname: alpha\ndescription: a\n---\n",
            "beta/SKILL.md": "---\nname: beta\ndescription: b\n---\n",
        }
    )
    result = local_skills.upload_custom_skill(category="tools", zip_bytes=z, filename="bundle.zip")
    assert result["status"] == 409
    assert not (skills_dir / "tools" / "beta").exists()


def test_upload_zip_without_skill_md_400_and_cleanup(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    z = _zip_bytes({"readme.txt": "no skill"})
    result = local_skills.upload_custom_skill(
        request_name="empty",
        zip_bytes=z,
        filename="empty.zip",
    )
    assert result["status"] == 400
    assert not (skills_dir / "empty").exists()


def test_extract_zip_rejects_path_traversal(tmp_path):
    z = _zip_bytes({"../escape.txt": "bad"})
    target = tmp_path / "skill"
    with pytest.raises(ValueError, match="非法"):
        extract_zip_and_flatten(z, target)


def test_handle_skillhub_upload_integration_only():
    handler = MagicMock()
    handler.headers = {
        "Content-Type": "application/json",
        "Content-Length": "0",
    }
    handler.rfile = MagicMock()
    with patch("integration.skills.handlers.integration_enabled", return_value=False):
        with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
            assert handle_skillhub_upload(handler) is True
            bad_fn.assert_called_once()
            assert bad_fn.call_args.kwargs.get("status") == 404


def test_handle_skillhub_upload_json_success():
    handler = MagicMock()
    body = b'{"name":"a","content":"---\\nname: a\\n---\\n"}'
    handler.headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    handler.rfile = MagicMock()
    handler.rfile.read.return_value = body
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.upload_custom_skill") as upload:
            with patch("integration.skills.handlers.j", return_value=True) as j_fn:
                upload.return_value = {
                    "ok": True,
                    "skill_count": 1,
                    "file_count": 1,
                    "skills": [{"name": "a", "dir_name": "a", "custom": True}],
                }
                assert handle_skillhub_upload(handler) is True
                upload.assert_called_once()
                j_fn.assert_called_once()
