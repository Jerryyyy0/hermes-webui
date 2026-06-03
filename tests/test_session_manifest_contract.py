"""Static contract tests for session manifest API and workspace inspector UI."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_routes_expose_session_manifest_endpoint():
    routes = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert '"/api/session/manifest"' in routes
    assert 'build_session_manifest' in routes


def test_workspace_panel_has_inspector_tabs():
    html = (REPO / 'static' / 'index.html').read_text(encoding='utf-8')
    assert 'workspaceTasksTab' in html
    assert 'workspaceReferencesTab' in html
    assert 'workspaceArtifactsTab' in html


def test_workspace_js_fetches_manifest():
    src = (REPO / 'static' / 'workspace.js').read_text(encoding='utf-8')
    assert '/api/session/manifest' in src
    assert 'loadSessionManifest' in src
    assert 'renderSessionInspector' in src
    assert 'HermesSessionInspector' in src
    assert 'applySessionManifestDelta' in src
    assert 'getTurnArtifacts' in src
    assert 'renderTurnArtifacts' in src


def test_messages_js_listens_for_manifest_delta():
    src = (REPO / 'static' / 'messages.js').read_text(encoding='utf-8')
    assert "source.addEventListener('manifest_delta'" in src
    assert 'HermesSessionInspector.applyDelta' in src


def test_ui_js_renders_turn_artifacts():
    src = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')
    assert 'data-turn-key' in src or 'dataset.turnKey' in src
    assert 'renderTurnArtifacts' in src


def test_session_manifest_module_has_extractors():
    src = (REPO / 'api' / 'session_manifest.py').read_text(encoding='utf-8')
    assert 'ARTIFACT_MUTATION_TOOLS' in src
    assert 'REFERENCE_READ_TOOLS' in src
    assert 'REFERENCE_DISCOVERY_TOOLS' in src
    assert 'def _extract_latest_todos' in src
    assert 'def _collect_tool_events' in src
    assert 'def extract_manifest_delta_from_tool_event' in src
    assert 'def merge_manifest_delta' in src


def test_session_manifest_doc_defines_sse_contract_and_tool_matrix():
    doc = (REPO / 'docs' / 'session-inspector-manifest.md').read_text(encoding='utf-8')
    assert 'manifest_delta' in doc
    assert '"version": 1' in doc
    assert '"sequence": 7' in doc
    assert '## 工具解析矩阵' in doc
    assert '`tool_start`' in doc
    assert '`tool_complete`' in doc
    assert '`todo`' in doc
    assert '`write_file`' in doc
    assert '`read_file`' in doc
