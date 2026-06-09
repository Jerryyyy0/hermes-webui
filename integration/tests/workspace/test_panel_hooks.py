"""Smoke tests for Integration Workspace Files UI seams."""

from pathlib import Path


def test_index_html_has_integration_workspace_panel():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert 'data-panel="integrationWorkspace"' in html
    assert "panelIntegrationWorkspace" in html
    assert "mainIntegrationWorkspace" in html
    assert "integrationWorkspaceFiles" in html
    assert "integrationWorkspaceSearch" in html
    assert "integrationWorkspaceTypeFilter" in html
    assert "integrationWorkspaceSort" in html
    assert "integrationWorkspaceOrder" in html
    assert "hermes_integration_workspace.js" in html
    assert "hermes_integration_workspace.css" in html


def test_panels_js_wires_integration_workspace():
    src = Path("static/panels.js").read_text(encoding="utf-8")
    assert "integrationWorkspace" in src
    assert "HermesIntegrationWorkspace" in src
    assert "'integrationWorkspace'" in src
    assert "panelIntegrationWorkspace" in src


def test_index_loads_integration_workspace_after_panels():
    html = Path("static/index.html").read_text(encoding="utf-8")
    panels_pos = html.index("static/panels.js")
    ws_pos = html.index("hermes_integration_workspace.js")
    assert panels_pos < ws_pos


def test_style_css_hides_integration_workspace_main_view_by_default():
    css = Path("static/style.css").read_text(encoding="utf-8")
    assert "main.main > #mainIntegrationWorkspace" in css
    assert "main.main.showing-integrationWorkspace > #mainIntegrationWorkspace" in css
    assert ":not(.showing-integrationWorkspace)" in css


def test_integration_workspace_js_calls_api():
    src = Path("integration/assets/hermes_integration_workspace.js").read_text(encoding="utf-8")
    assert "API_PREFIX" in src
    assert "listQueryParams" in src
    assert "${API_PREFIX}/files" in src
    assert "${API_PREFIX}/file" in src
    assert "has_more" in src
    assert "integrationWorkspaceFiles" in src


def test_integration_workspace_does_not_auto_preview_on_panel_open():
    src = Path("integration/assets/hermes_integration_workspace.js").read_text(encoding="utf-8")
    assert "clearIwsPreview" in src
    assert "selectFile(saved, { preview: false })" in src


def test_routes_inject_integration_workspace_files_flag():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    assert "__INTEGRATION_WORKSPACE_FILES__" in src
    assert "_integration_workspace_files_flag" in src
