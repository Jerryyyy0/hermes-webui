"""Smoke tests for Integration Cron UI seams."""

from pathlib import Path


def test_index_html_has_integration_crons_panel():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert 'data-panel="integrationCrons"' in html
    assert "panelIntegrationCrons" in html
    assert "mainIntegrationCrons" in html
    assert "integrationCronAllProfiles" in html
    assert "hermes_integration_crons.js" in html


def test_panels_js_wires_integration_crons():
    src = Path("static/panels.js").read_text(encoding="utf-8")
    assert "integrationCrons" in src
    assert "HermesCronShared" in src
    assert "all_profiles=1" in src
    assert "openCronRunSession" in src


def test_index_loads_integration_crons_after_panels():
    html = Path("static/index.html").read_text(encoding="utf-8")
    panels_pos = html.index("static/panels.js")
    crons_pos = html.index("hermes_integration_crons.js")
    assert panels_pos < crons_pos


def test_style_css_hides_integration_crons_main_view_by_default():
    css = Path("static/style.css").read_text(encoding="utf-8")
    assert "main.main > #mainIntegrationCrons" in css
    assert "main.main.showing-integrationCrons > #mainIntegrationCrons" in css
    assert ":not(.showing-integrationCrons)" in css


def test_cron_hub_run_rows_can_open_materialized_sessions():
    src = Path("integration/assets/hermes_integration_crons.js").read_text(encoding="utf-8")
    assert 'data-action="embed-session"' in src
    assert "toggleEmbeddedSession" in src
    assert "/api/session?session_id=" in src
    assert "data.session_id" in src
    assert "openSession(data.session_id)" in src
