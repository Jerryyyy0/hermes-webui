"""Custom skill preview (local doc/structure/file) tests."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.skills.handlers import try_handle_get
from integration.skills.local_skills import get_custom_doc, get_custom_file, get_custom_structure


def test_get_custom_doc_reads_shared_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "audit" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# Hello", encoding="utf-8")

    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    payload = get_custom_doc("my-skill")
    assert payload["content"].startswith("---")
    assert "Hello" in payload["content"]


def test_get_custom_structure_lists_scripts_and_references(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "audit" / "my-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text("# Guide", encoding="utf-8")

    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    payload = get_custom_structure("my-skill")
    assert payload["scripts"] == [{"path": "scripts/run.py"}]
    assert payload["references"] == [{"path": "references/guide.md"}]


def test_get_custom_file_rejects_traversal(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")

    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    payload = get_custom_file("my-skill", "../secret.txt")
    assert payload["status"] == 404


def test_skillhub_content_custom_scope_routes_local():
    parsed = urlparse("/api/skillhub/content?name=my-skill&scope=custom")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.get_custom_doc") as get_doc:
            with patch("integration.skills.handlers.j", return_value=True) as j_fn:
                get_doc.return_value = {"name": "my-skill", "content": "# Hi", "linked_files": {}}
                assert try_handle_get(handler, parsed) is True
                get_doc.assert_called_once_with("my-skill")
                j_fn.assert_called_once()


def test_skillhub_content_custom_scope_not_found():
    parsed = urlparse("/api/skillhub/content?name=missing&scope=custom")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.get_custom_doc") as get_doc:
            with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
                get_doc.return_value = {"error": "Skill not found", "status": 404}
                assert try_handle_get(handler, parsed) is True
                bad_fn.assert_called_once()
                assert bad_fn.call_args[0][2] == 404
