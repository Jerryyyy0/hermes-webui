"""Static contract tests for session manifest API and workspace inspector UI."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_routes_expose_session_manifest_endpoint():
    routes = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert '"/api/session/manifest"' in routes
    assert 'build_session_manifest' in routes
    assert '"/api/file/allowlisted"' not in routes


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
    assert 'refreshTurnArtifactsInChat' in src
    assert 'openManifestPreview' in src
    assert 'openIntegrationFilePreview' in src
    assert 'openSkillContentPreview' in src
    assert 'isManifestPreviewable' in src
    assert 'INTEGRATION_WORKSPACE_API' in src
    assert '${INTEGRATION_WORKSPACE_API}/file' in src
    assert '/api/skillhub/content' in src
    assert '/api/file/allowlisted' not in src
    assert '_isManifestAbsolutePath' in src
    assert '_manifestFilePreviewUrl' in src
    assert 'api/media?path=' in src
    assert 'key.startsWith(\'turn:\')' in src


def test_messages_js_listens_for_manifest_delta():
    src = (REPO / 'static' / 'messages.js').read_text(encoding='utf-8')
    assert "source.addEventListener('manifest_delta'" in src
    assert 'HermesSessionInspector.applyDelta' in src
    manifest_delta_block = src.split("source.addEventListener('manifest_delta'", 1)[1].split('});', 1)[0]
    assert '.turn-artifacts' not in manifest_delta_block


def test_ui_js_renders_turn_artifacts():
    src = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')
    assert 'data-turn-key' in src or 'dataset.turnKey' in src
    assert 'renderTurnArtifacts' in src
    assert 'liveAssistantTurn' in src
    assert 'data-live-assistant' in src


def test_session_manifest_module_has_extractors():
    src = (REPO / 'api' / 'session_manifest.py').read_text(encoding='utf-8')
    assert 'ARTIFACT_MUTATION_TOOLS' in src
    assert 'REFERENCE_READ_TOOLS' in src
    assert 'REFERENCE_SKILL_TOOLS' in src
    assert 'def _serialize_manifest_row' in src
    assert 'def _rows_to_wire' in src
    assert 'def _extract_latest_todos' in src
    assert 'def _collect_tool_events' in src
    assert 'def extract_manifest_delta_from_tool_event' in src
    assert 'def merge_manifest_delta' in src


def test_session_manifest_store_lifecycle_hooks_present():
    src = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert 'delete_session_manifest_records' in src
    assert 'delete_session_manifest_turns' in src
    assert 's.turn_artifacts = {}' in src


def test_session_manifest_store_module_contract():
    src = (REPO / 'api' / 'session_manifest_store.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE IF NOT EXISTS session_manifest_records' in src
    assert 'UNIQUE(lineage_key, profile, turn_key, record_kind, path)' in src
    assert 'def resolve_manifest_lineage_key' in src
    assert 'def upsert_manifest_records' in src
    assert 'def load_manifest_records' in src
    assert 'def delete_session_manifest_records' in src
    assert 'def delete_session_manifest_turns' in src
    assert 'ASSISTANT_PROSE_SOURCE_TOOL = "assistant_prose"' in src


def test_chat_start_passes_current_turn_key_to_stream_worker():
    src = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    block = src.split('_prepare_chat_start_session_for_stream(', 1)[1].split('break', 1)[0]
    assert 'stream_turn_key = _turn_key_for_pending_user_message(s, msg)' in block
    assert 'worker_kwargs = {"model_provider": model_provider, "stream_turn_key": stream_turn_key}' in src


def test_streaming_manifest_turn_key_fallback_uses_last_user_key_first():
    src = (REPO / 'api' / 'streaming.py').read_text(encoding='utf-8')
    fallback_block = src.split("_manifest_turn_key = str(stream_turn_key or '').strip()", 1)[1]
    last_user_lookup = fallback_block.index("for _m in reversed(getattr(s, 'messages', None) or [])")
    next_key_lookup = fallback_block.index('_manifest_turn_key = _next_turn_key(')
    assert last_user_lookup < next_key_lookup
    assert "_m.get('role') == 'user'" in fallback_block
    assert "_m.get('_turn_key', '')" in fallback_block


def test_session_manifest_doc_defines_sse_contract_and_tool_matrix():
    doc = (REPO / 'docs' / 'session-inspector-manifest.md').read_text(encoding='utf-8')
    assert 'manifest_delta' in doc
    assert '"version": 1' in doc
    assert '"sequence": 7' in doc
    assert '"turn_key": "turn:' in doc
    assert 'done' in doc
    assert '聊天区' in doc
    assert '## 工具解析矩阵' in doc
    assert '`tool_start`' in doc
    assert '`tool_complete`' in doc
    assert '`todo`' in doc
    assert '`write_file`' in doc
    assert '`read_file`' in doc
