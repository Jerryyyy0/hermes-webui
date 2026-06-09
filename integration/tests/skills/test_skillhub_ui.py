"""Smoke tests for SkillHub edit UI seams."""

from pathlib import Path


def test_index_html_has_skillhub_edit_buttons():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert "btnSkillhubEdit" in html
    assert "btnSkillhubCancelEdit" in html
    assert "btnSkillhubSaveEdit" in html
    assert "HermesSkillHub.editCurrent" in html
    assert "HermesSkillHub.saveEditForm" in html


def test_index_html_has_skillhub_download_button():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert "btnSkillhubDownload" in html
    assert "HermesSkillHub.downloadCurrent" in html


def test_skillhub_js_wires_edit_api():
    src = Path("integration/assets/hermes_skillhub.js").read_text(encoding="utf-8")
    assert "/api/skillhub/edit" in src
    assert "/api/skillhub/download" in src
    assert "downloadCurrent" in src
    assert "editCurrent" in src
    assert "saveEditForm" in src
    assert "cancelEditForm" in src
    assert "btnSkillhubEdit" in src
    assert "btnSkillhubDownload" in src
    assert "overwrite" in src
