"""Static contract tests for session manifest API and workspace inspector UI."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_routes_expose_session_manifest_endpoint():
    routes = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert '"/api/session/manifest"' in routes
    assert 'build_session_manifest' in routes
    assert 'manifest_source' in routes
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
    assert 'api/media?path=' not in src
    assert "_previewSource = _isManifestAbsolutePath(path) ? 'manifest-external' : 'workspace';" in src
    assert 'key.startsWith(\'turn:\')' in src
    assert 'function _mergeManifestReferences' in src
    assert "kind==='knowledge_base_document'" in src
    assert 'metadata.page_content' in src


def test_messages_js_listens_for_manifest_delta():
    src = (REPO / 'static' / 'messages.js').read_text(encoding='utf-8')
    assert "source.addEventListener('manifest_delta'" in src
    assert 'HermesSessionInspector.applyDelta' in src
    manifest_delta_block = src.split("source.addEventListener('manifest_delta'", 1)[1].split('});', 1)[0]
    assert '.turn-artifacts' not in manifest_delta_block


def test_workspace_js_renders_turn_artifacts_by_stable_turn_key():
    src = (REPO / 'static' / 'workspace.js').read_text(encoding='utf-8')
    assert 'function getTurnArtifacts(turnKey)' in src
    assert 'row=>row&&row.turn_key===turnKey' in src
    assert 'function renderTurnArtifacts' in src
    assert 'data-turn-key' in src or 'dataset.turnKey' in src


def test_ui_stamps_assistant_turn_with_owning_user_turn_key():
    src = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')
    assert "currentAssistantTurn.dataset.turnKey=currentTurnKey" in src


def test_session_manifest_module_has_extractors():
    src = (REPO / 'integration' / 'session_manifest' / 'manifest.py').read_text(encoding='utf-8')
    assert 'ARTIFACT_MUTATION_TOOLS' in src
    assert 'ARTIFACT_EXCLUSION_READ_TOOLS' in src
    assert 'REFERENCE_SKILL_TOOLS' in src
    assert 'EXECUTION_ARTIFACT_TOOLS' in src
    assert 'def _terminal_output_paths' in src
    assert 'def _serialize_manifest_row' in src
    assert 'def _rows_to_wire' in src
    assert 'def _extract_latest_todos' in src
    assert 'def _collect_tool_events' in src
    assert 'def extract_manifest_delta_from_tool_event' in src
    assert 'def merge_manifest_delta' in src
    assert 'integration.knowledge_base.turn_references' in src
    assert 'def _merge_reference_wire_rows' in src


def test_session_manifest_store_lifecycle_hooks_present():
    src = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert 'delete_session_manifest_records' in src
    assert 'delete_session_manifest_turns' in src
    assert 's.turn_artifacts = {}' in src


def test_session_manifest_store_module_contract():
    src = (REPO / 'integration' / 'session_manifest' / 'store.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE IF NOT EXISTS session_manifest_records' in src
    assert 'UNIQUE(lineage_key, profile, turn_key, record_kind, path, workspace_root)' in src
    assert 'def resolve_manifest_lineage_key' in src
    assert 'def upsert_manifest_records' in src
    assert 'def load_manifest_records' in src
    assert 'def backfill_missing_manifest_records' in src
    assert 'def delete_session_manifest_records' in src
    assert 'def delete_session_manifest_turns' in src
    assert 'rebind_manifest_turn_records' in src
    assert 'ASSISTANT_PROSE_SOURCE_TOOL = "assistant_prose"' in src


def test_chat_start_binds_persisted_turn_key_to_stream_worker():
    src = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert 's.pending_turn_key = str(turn_key or \'\').strip() or None' in src
    assert 'stream_turn_key = str(getattr(s, "pending_turn_key", "") or "").strip()' in src
    worker_block = src.split('worker_kwargs = {', 1)[1].split('}', 1)[0]
    assert '"stream_turn_key": stream_turn_key' in worker_block


def test_streaming_manifest_turn_key_uses_bound_key_without_transcript_guessing():
    src = (REPO / 'api' / 'streaming.py').read_text(encoding='utf-8')
    assert "_manifest_turn_key = str(stream_turn_key or getattr(s, 'pending_turn_key', '') or '').strip()" in src
    assert '_next_turn_key as _ntk' not in src
    persist_block = src.split('def _persist_turn_artifact_paths', 1)[1].split('\ndef ', 1)[0]
    assert '_stream_artifact_evidence' in persist_block
    assert "'stage': 'stale_worker'" in persist_block
    assert "'stage': 'transcript_unavailable'" in persist_block
    assert 'upsert_manifest_records' in persist_block
    assert "'status': 'persisted'" in persist_block
    assert 'turn_artifacts' not in persist_block

    routes = (REPO / 'api' / 'routes.py').read_text(encoding='utf-8')
    assert 'stream_turn_key = _turn_key_for_pending_user_message(s, msg)' not in routes


def test_gateway_uses_canonical_turn_for_merge_and_settlement():
    src = (REPO / 'api' / 'gateway_chat.py').read_text(encoding='utf-8')
    assert 'canonical_turn_key=manifest_turn_key' in src
    assert 'expected_user_text=msg_text' in src


def test_completed_transcript_is_saved_before_manifest_decision_and_settlement_owns_journal():
    src = (REPO / 'api' / 'streaming.py').read_text(encoding='utf-8')
    block = src.split('Make the completed transcript durable before publishing', 1)[1]
    save_index = block.index('s.save()')
    manifest_index = block.index('_artifact_decision = _persist_turn_artifact_paths(')
    completed_index = block.index('"event": "completed"')
    settlement_block = src.split('def _finalize_artifact_settlement(', 1)[1].split('\ndef ', 1)[0]
    assert save_index < manifest_index < completed_index
    assert "stream_id=stream_id" in block[manifest_index:completed_index]
    assert "terminal_reason='completed'" in block[manifest_index:completed_index]
    assert "'event': 'artifact_persistence_failed'" in settlement_block
    assert "_artifact_decision.get('status') == 'persisted'" in block


def test_session_manifest_docs_assign_public_contract_to_api():
    api = (REPO / 'docs' / 'api' / 'session-manifest-api.md').read_text(encoding='utf-8')
    artifacts = (REPO / 'docs' / 'architecture' / 'session-manifest-artifacts.md').read_text(encoding='utf-8')

    assert 'GET `/api/session/manifest`' in api
    assert 'Manifest 的定位与资源边界' in api
    assert '派生索引' in api
    assert 'artifacts > references' in api
    assert '"version": 1' in api
    assert '"sequence": 7' in api
    assert '"turn_key": "turn:' in api
    assert '顶层不携带工具调用来源' in api
    assert 'mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments' in api
    assert 'mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross' in api
    assert '"page_content"' in api
    assert '无需新增数据库表' in api

    assert 'Decision-first 构建流程' in artifacts
    assert '默认 workspace 与 Artifact 根' in artifacts
    assert 'artifact_workspace_root_for_session' in artifacts
    assert 'write_file' in artifacts
    assert '`terminal`' in artifacts
    assert 'stdout' in artifacts
    assert '最后一条 assistant' in artifacts
    assert 'ARTIFACT_EXCLUSION_READ_TOOLS' in artifacts
    assert '不进入顶层/per-turn references' in artifacts
    assert '只抑制同 turn、同 path 的 `assistant_prose`' in artifacts
    assert '完整 JSON' not in artifacts
    assert 'event: manifest_delta' not in artifacts

    assert not (REPO / 'docs' / 'session-manifest-artifact-regex.md').exists()
    assert not (REPO / 'docs' / 'session-manifest-artifacts-entry.md').exists()
