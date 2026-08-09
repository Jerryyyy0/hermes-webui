import json
from pathlib import Path
from types import SimpleNamespace

from api import session_manifest_store as store


def _session(session_id, *, profile='ops', parent_session_id=None, pre_compression_snapshot=False):
    return SimpleNamespace(
        session_id=session_id,
        profile=profile,
        parent_session_id=parent_session_id,
        pre_compression_snapshot=pre_compression_snapshot,
    )


def test_store_upsert_uses_session_profile_and_normalizes_source_tool(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeprof01', profile='ops')

    rows = store.upsert_manifest_records(
        session,
        'turn:1',
        [{
            'path': 'report.md',
            'source_tool': '',
            'profile': 'ignored',
        }],
        db_path=db_path,
    )

    assert rows[0]['profile'] == 'ops'
    assert rows[0]['source_tool'] == 'assistant_prose'
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert loaded[0]['profile'] == 'ops'
    assert loaded[0]['source_tool'] == 'assistant_prose'


def test_store_missing_profile_is_empty_string(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeemptyprof01', profile=None)

    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(session, db_path=db_path)
    assert loaded[0]['profile'] == ''


def test_store_keeps_same_path_separate_by_profile(tmp_path):
    db_path = tmp_path / 'manifest.db'
    store.upsert_manifest_records(
        _session('storesamepath01', profile='ops'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        _session('storesamepath02', profile='research'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    assert len(store.load_manifest_records(_session('storesamepath01', profile='ops'), db_path=db_path)) == 1
    assert len(store.load_manifest_records(_session('storesamepath02', profile='research'), db_path=db_path)) == 1


def test_project_artifact_path_for_integration_root_prefixes_child_session(tmp_path):
    base = tmp_path / 'workspace-base'
    child = base / 'sid123'
    child.mkdir(parents=True)

    assert store.relative_prefix_under_root(child, base) == 'sid123'
    assert store.relative_prefix_under_root(base, base) == ''
    assert store.relative_prefix_under_root('', base) is None
    assert store.relative_prefix_under_root(tmp_path / 'other', base) is None

    assert store.project_artifact_path_for_integration_root('report.md', child, integration_root=base) == (
        'sid123/report.md'
    )
    assert store.project_artifact_path_for_integration_root('report.md', base, integration_root=base) == 'report.md'
    assert store.project_artifact_path_for_integration_root('report.md', '', integration_root=base) == 'report.md'
    assert store.project_artifact_path_for_integration_root(
        'report.md', tmp_path / 'other', integration_root=base,
    ) == 'report.md'
    assert store.project_artifact_path_for_integration_root(
        '/abs/media.png', child, integration_root=base,
    ) == '/abs/media.png'


def test_legacy_blank_root_resolves_to_default_workspace_for_projection(tmp_path, monkeypatch):
    base = tmp_path / 'workspace-base'
    base.mkdir()
    monkeypatch.setattr('api.workspace._BOOT_DEFAULT_WORKSPACE', base)

    assert store.effective_manifest_workspace_root('', legacy_root=base) == base.resolve()
    assert store.relative_prefix_under_root('', base) == ''
    assert store.project_artifact_path_for_integration_root(
        'report.md', '', integration_root=base,
    ) == 'report.md'


def test_manifest_file_wire_paths_use_the_record_workspace_and_skip_external_roots(tmp_path, monkeypatch):
    from api.session_manifest import _rows_to_wire

    base = tmp_path / 'workspace-base'
    inside = base / 'project-a'
    outside = tmp_path / 'project-b'
    inside.mkdir(parents=True)
    outside.mkdir()
    (inside / 'report.md').write_text('inside', encoding='utf-8')
    (outside / 'report.md').write_text('outside', encoding='utf-8')
    monkeypatch.setattr('api.workspace._BOOT_DEFAULT_WORKSPACE', base)

    wire = _rows_to_wire([
        {
            'path': 'report.md',
            'source_tool': 'write_file',
            'preview': 'file',
            'turn_key': 'turn:1',
            'workspace_root': str(inside),
        },
        {
            'path': 'report.md',
            'source_tool': 'write_file',
            'preview': 'file',
            'turn_key': 'turn:2',
            'workspace_root': str(outside),
        },
    ], base)

    assert wire == [{
        'path': 'project-a/report.md',
        'preview': 'file',
        'source_tool': 'write_file',
    }]


def test_empty_decisions_are_scoped_to_their_effective_workspace_root(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    base = tmp_path / 'workspace-base'
    first = base / 'first'
    second = base / 'second'
    first.mkdir(parents=True)
    second.mkdir()
    first_session = _session('decisionfirst01')
    second_session = _session('decisionsecond02')
    first_session.workspace = str(first)
    second_session.workspace = str(second)
    monkeypatch.setattr(store, 'resolve_manifest_lineage_key', lambda _session: 'shared-lineage')

    store.upsert_manifest_records(
        first_session, 'turn:1', [{'path': '', 'source_tool': 'assistant_prose'}], db_path=db_path,
    )
    store.upsert_manifest_records(
        second_session, 'turn:1', [{'path': 'report.md', 'source_tool': 'write_file'}], db_path=db_path,
    )

    assert store.load_manifest_empty_turn_keys(first_session, db_path=db_path) == {'turn:1'}
    assert store.load_manifest_empty_turn_keys(second_session, db_path=db_path) == set()
    assert store.load_manifest_decided_turn_keys_by_root(first_session, db_path=db_path) == {
        ('turn:1', str(first.resolve())),
        ('turn:1', str(second.resolve())),
    }


def test_default_root_aliases_are_deduped_and_replaced_without_database_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    base = tmp_path / 'workspace-base'
    base.mkdir()
    legacy = _session('legacyalias01')
    canonical = _session('canonicalalias02')
    canonical.workspace = str(base)
    monkeypatch.setattr('api.workspace._BOOT_DEFAULT_WORKSPACE', base)
    monkeypatch.setattr(store, 'resolve_manifest_lineage_key', lambda _session: 'shared-lineage')

    store.upsert_manifest_records(
        legacy, 'turn:1', [{'path': 'report.md', 'source_tool': 'assistant_prose'}], db_path=db_path,
    )
    store.upsert_manifest_records(
        canonical, 'turn:1', [{'path': 'report.md', 'source_tool': 'write_file'}], db_path=db_path,
    )

    loaded = store.load_manifest_records(canonical, db_path=db_path)
    assert [(row['path'], row['source_tool'], row['workspace_root']) for row in loaded] == [
        ('report.md', 'write_file', str(base.resolve())),
    ]

    store.replace_manifest_turn_records(
        canonical, 'turn:1', [{'path': 'final.md', 'source_tool': 'write_file'}], db_path=db_path,
    )
    assert [(row['path'], row['workspace_root']) for row in store.load_manifest_records(canonical, db_path=db_path)] == [
        ('final.md', str(base.resolve())),
    ]


def test_workspace_profile_index_keeps_same_artifact_name_separate_by_root(tmp_path):
    db_path = tmp_path / 'manifest.db'
    base = tmp_path / 'workspace-base'
    first = base / 'first'
    second = base / 'second'
    first.mkdir(parents=True)
    second.mkdir()
    (first / 'report.md').write_text('first', encoding='utf-8')
    (second / 'report.md').write_text('second', encoding='utf-8')

    first_session = SimpleNamespace(
        session_id='workspaceone01', profile='ops', parent_session_id=None,
        pre_compression_snapshot=False, workspace=str(first),
    )
    second_session = SimpleNamespace(
        session_id='workspacetwo02', profile='research', parent_session_id=None,
        pre_compression_snapshot=False, workspace=str(second),
    )
    store.upsert_manifest_records(
        first_session, 'turn:1', [{'path': 'report.md', 'source_tool': 'write_file'}], db_path=db_path,
    )
    store.upsert_manifest_records(
        second_session, 'turn:1', [{'path': 'report.md', 'source_tool': 'write_file'}], db_path=db_path,
    )

    assert store.get_artifact_profile_index(base, db_path=db_path) == {
        'first/report.md': 'ops',
        'second/report.md': 'research',
    }
    assert store.get_artifact_paths_for_profile('ops', base, db_path=db_path) == frozenset({'first/report.md'})
    assert store.get_artifact_paths_for_profile('research', base, db_path=db_path) == frozenset({'second/report.md'})


def test_manifest_store_migrates_old_relative_path_schema_as_default_workspace_legacy(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            '''
            CREATE TABLE session_manifest_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              lineage_key TEXT NOT NULL,
              profile TEXT NOT NULL DEFAULT '',
              turn_key TEXT NOT NULL,
              record_kind TEXT NOT NULL,
              path TEXT NOT NULL,
              preview TEXT NOT NULL,
              source_tool TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(lineage_key, profile, turn_key, record_kind, path)
            )
            '''
        )
        conn.execute(
            '''
            INSERT INTO session_manifest_records (
              session_id, lineage_key, profile, turn_key, record_kind,
              path, preview, source_tool, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            ('legacyroot01', 'legacyroot01', 'ops', 'turn:1', 'artifact', 'report.md', 'file', 'write_file', 1.0, 1.0),
        )

    with store._connect(db_path) as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(session_manifest_records)').fetchall()}
        row = conn.execute('SELECT workspace_root, path FROM session_manifest_records').fetchone()
    assert 'workspace_root' in columns
    assert dict(row) == {'workspace_root': '', 'path': 'report.md'}
    monkeypatch.setattr('api.workspace._BOOT_DEFAULT_WORKSPACE', tmp_path)
    # Legacy rows remain untouched in SQLite but are read relative to the
    # boot-time default workspace.
    assert store.get_artifact_profile_index(tmp_path, db_path=db_path) == {'report.md': 'ops'}


def test_compression_child_reads_parent_lineage_records(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    (session_dir / 'parent01.json').write_text(
        '{"session_id":"parent01","pre_compression_snapshot":true,"messages":[]}',
        encoding='utf-8',
    )
    (session_dir / 'child01.json').write_text(
        '{"session_id":"child01","parent_session_id":"parent01","messages":[]}',
        encoding='utf-8',
    )
    monkeypatch.setattr(store, 'SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session('parent01', profile='ops', pre_compression_snapshot=True),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(
        _session('child01', profile='ops', parent_session_id='parent01'),
        db_path=db_path,
    )
    assert [row['path'] for row in loaded] == ['report.md']


def test_plain_fork_does_not_read_parent_records(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    (session_dir / 'parentfork01.json').write_text(
        '{"session_id":"parentfork01","pre_compression_snapshot":false,"messages":[]}',
        encoding='utf-8',
    )
    (session_dir / 'childfork01.json').write_text(
        '{"session_id":"childfork01","parent_session_id":"parentfork01","messages":[]}',
        encoding='utf-8',
    )
    monkeypatch.setattr(store, 'SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session('parentfork01', profile='ops'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(
        _session('childfork01', profile='ops', parent_session_id='parentfork01'),
        db_path=db_path,
    )
    assert loaded == []


def test_delete_session_manifest_turns_prunes_invisible_turns(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeprune01', profile='ops')
    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        session,
        'turn:2',
        [{'path': 'two.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    store.delete_session_manifest_turns('storeprune01', {'turn:1'}, db_path=db_path)

    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [row['path'] for row in loaded] == ['one.md']


def _write_session_sidecar(session_dir, sid, *, profile=None):
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        'session_id': sid,
        'title': 'Test',
        'created_at': 1.0,
        'updated_at': 2.0,
        'profile': profile,
        'messages': [],
    }
    (session_dir / f'{sid}.json').write_text(
        json.dumps(payload),
        encoding='utf-8',
    )


def test_backfill_empty_profile_patches_rows_with_session_profile(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    sid = 'backfill01'
    monkeypatch.setattr('api.models.SESSION_DIR', session_dir)
    monkeypatch.setattr('api.config.SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:2',
        [{'path': 'two.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    _write_session_sidecar(session_dir, sid, profile='ops')

    result = store.backfill_empty_profile_artifacts(db_path=db_path)

    assert result == {'scanned': 1, 'patched': 2, 'skipped': 0}
    loaded = store.load_manifest_records(_session(sid, profile='ops'), db_path=db_path)
    assert len(loaded) == 2
    assert all(row['profile'] == 'ops' for row in loaded)


def test_backfill_empty_profile_skips_when_session_profile_also_empty(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    sid = 'backfill02'
    monkeypatch.setattr('api.models.SESSION_DIR', session_dir)
    monkeypatch.setattr('api.config.SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    _write_session_sidecar(session_dir, sid, profile=None)

    result = store.backfill_empty_profile_artifacts(db_path=db_path)

    assert result == {'scanned': 1, 'patched': 0, 'skipped': 1}
    loaded = store.load_manifest_records(_session(sid, profile=None), db_path=db_path)
    assert loaded[0]['profile'] == ''


def _bclass_session(sid, *, workspace, profile='ops', messages=None, turn_artifacts=None):
    """A session-like object with the attributes backfill_session_artifacts reads."""
    return SimpleNamespace(
        session_id=sid,
        profile=profile,
        parent_session_id=None,
        pre_compression_snapshot=False,
        workspace=str(workspace),
        messages=messages or [],
        tool_calls=[],
        turn_artifacts=turn_artifacts,
    )


def _write_file_turn_messages(turn_key, filename):
    """Build a user+assistant(tool_use)+tool(result) turn that writes a file."""
    return [
        {'role': 'user', 'content': 'write a report', '_turn_key': turn_key},
        {
            'role': 'assistant',
            'content': [
                {
                    'type': 'tool_use',
                    'id': 'toolu_1',
                    'name': 'write_file',
                    'input': {'path': filename, 'content': 'hello'},
                },
            ],
        },
        {
            'role': 'tool',
            'tool_call_id': 'toolu_1',
            'content': f'wrote {filename}',
        },
    ]


def test_backfill_session_artifacts_extracts_from_messages_without_turn_artifacts(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass01', workspace=workspace, profile='ops', messages=messages,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 1
    assert result['skipped'] == 0
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    row = loaded[0]
    assert row['path'] == 'report.md'
    assert row['profile'] == 'ops'
    assert row['turn_key'] == 'turn:1'
    assert row['source_tool'] == 'write_file'
    assert row['preview'] == 'file'


def test_backfill_session_artifacts_can_scan_messages_even_with_turn_artifacts(tmp_path):
    """The low-level scanner ignores legacy JSON and reuses manifest extraction rules."""
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass02', workspace=workspace, profile='ops', messages=messages,
        turn_artifacts={'turn:1': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': 'file'}]},
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    # Re-extraction still runs; upsert is idempotent so the row lands once.
    assert result['written'] == 1
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    assert loaded[0]['path'] == 'report.md'


def test_backfill_session_artifacts_idempotent(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass03', workspace=workspace, profile='ops', messages=messages,
    )

    first = store.backfill_session_artifacts(session, db_path=db_path)
    second = store.backfill_session_artifacts(session, db_path=db_path)

    assert first['written'] == 1
    # Second run re-extracts the same rows; upsert ON CONFLICT keeps them
    # stable — no duplicate rows, count stays at 1.
    loaded_after_first = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded_after_first) == 1
    store.backfill_session_artifacts(session, db_path=db_path)
    loaded_after_second = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded_after_second) == 1


def test_backfill_session_artifacts_captures_prose_delivered_paths(tmp_path):
    """Files delivered only in assistant prose (no write_file tool call) are
    captured by the prose-path scan, mirroring _persist_turn_artifact_paths."""
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'summary.md').write_text('hi', encoding='utf-8')

    # A turn with no write_file tool call; the assistant explicitly delivers the
    # filename in prose. The file exists on disk, so the delivery scan should pick
    # it up.
    messages = [
        {'role': 'user', 'content': 'summarize', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': '文件位置：summary.md'},
    ]
    session = _bclass_session(
        'bclass04', workspace=workspace, profile='ops', messages=messages,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 1
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    row = loaded[0]
    assert row['path'] == 'summary.md'
    assert row['profile'] == 'ops'
    assert row['source_tool'] == 'assistant_prose'
    assert row['preview'] == 'file'


def test_backfill_session_artifacts_ignores_tool_result_basenames(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '1.docx').write_text('docx-like', encoding='utf-8')
    (workspace / 'SKILL.md').write_text('# skill', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'inspect terminal output', '_turn_key': 'turn:1'},
        {
            'role': 'assistant',
            'content': [
                {
                    'type': 'tool_use',
                    'id': 'toolu_terminal',
                    'name': 'terminal',
                    'input': {'command': 'printf "1.docx SKILL.md"'},
                },
            ],
        },
        {'role': 'tool', 'tool_call_id': 'toolu_terminal', 'content': 'created 1.docx and read SKILL.md'},
        {'role': 'assistant', 'content': 'Terminal finished.'},
    ]
    session = _bclass_session(
        'bclass05', workspace=workspace, profile='ops', messages=messages,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 1
    assert store.load_manifest_records(session, db_path=db_path) == []
    assert store.load_manifest_decided_turn_keys(session, db_path=db_path) == {'turn:1'}


def test_repair_empty_manifest_turn_replaces_only_empty_decision(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'final.html').write_text('<html></html>', encoding='utf-8')
    (workspace / 'stored.md').write_text('stored', encoding='utf-8')
    session = _bclass_session(
        'repair_empty01',
        workspace=workspace,
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'done'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:3'},
            {'role': 'assistant', 'content': '| `final.html` | report |'},
        ],
    )
    store.upsert_manifest_records(
        session, 'turn:1', [{'path': 'stored.md', 'source_tool': 'write_file'}], db_path=db_path,
    )
    store.upsert_manifest_records(
        session, 'turn:3', [{'path': '', 'source_tool': 'assistant_prose'}], db_path=db_path,
    )

    assert store.repair_empty_manifest_turns(session, db_path=db_path) == 1
    assert store.repair_empty_manifest_turns(session, db_path=db_path) == 0

    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [(row['turn_key'], row['path'], row['source_tool']) for row in loaded] == [
        ('turn:1', 'stored.md', 'write_file'),
        ('turn:3', 'final.html', 'assistant_prose'),
    ]
    assert store.load_manifest_empty_turn_keys(session, db_path=db_path) == set()


def test_backfill_session_artifacts_skips_decided_empty_turn(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.docx').write_text('docx-like', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'make report', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': '文件位置：report.docx'},
    ]
    session = _bclass_session(
        'bclass06', workspace=workspace, profile='ops', messages=messages,
    )
    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': '', 'source_tool': 'assistant_prose', 'preview': 'file'}],
        db_path=db_path,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 0
    assert store.load_manifest_records(session, db_path=db_path) == []
    assert store.load_manifest_decided_turn_keys(session, db_path=db_path) == {'turn:1'}


def test_backfill_session_artifacts_skips_whole_session_when_any_turn_decided(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'stored.docx').write_text('stored', encoding='utf-8')
    (workspace / 'later.docx').write_text('later', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'first', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': '文件：stored.docx'},
        {'role': 'user', 'content': 'second', '_turn_key': 'turn:3'},
        {'role': 'assistant', 'content': '文件：later.docx'},
    ]
    session = _bclass_session(
        'bclass07', workspace=workspace, profile='ops', messages=messages,
    )
    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'stored.docx', 'source_tool': 'assistant_prose', 'preview': 'file'}],
        db_path=db_path,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result == {'written': 0, 'skipped': 1, 'turns': 0}
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [row['path'] for row in loaded] == ['stored.docx']
    assert store.load_manifest_decided_turn_keys(session, db_path=db_path) == {'turn:1'}


def test_backfill_missing_manifest_records_keeps_existing_db_authority(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'stored.docx').write_text('stored', encoding='utf-8')
    (workspace / 'legacy.docx').write_text('legacy', encoding='utf-8')
    (workspace / 'later.docx').write_text('later', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'first', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': '文件：later.docx'},
    ]
    session = _bclass_session(
        'bclass08',
        workspace=workspace,
        profile='ops',
        messages=messages,
        turn_artifacts={'turn:1': [{'path': 'legacy.docx', 'source_tool': 'write_file', 'preview': 'file'}]},
    )
    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'stored.docx', 'source_tool': 'write_file', 'preview': 'file'}],
        db_path=db_path,
    )

    result = store.backfill_missing_manifest_records(session, db_path=db_path)

    assert result == {'source': 'db', 'written': 0, 'skipped': 1, 'turns': 0}
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [row['path'] for row in loaded] == ['stored.docx']


def test_backfill_missing_manifest_records_prefers_legacy_json_when_db_empty(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'legacy.docx').write_text('legacy', encoding='utf-8')
    (workspace / 'later.docx').write_text('later', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'first', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': '文件：later.docx'},
    ]
    session = _bclass_session(
        'bclass09',
        workspace=workspace,
        profile='ops',
        messages=messages,
        turn_artifacts={'turn:1': [{'path': 'legacy.docx', 'source_tool': 'write_file', 'preview': 'file'}]},
    )

    result = store.backfill_missing_manifest_records(session, db_path=db_path)

    assert result == {'source': 'backfill', 'written': 1, 'skipped': 0, 'turns': 1}
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [(row['turn_key'], row['path'], row['source_tool']) for row in loaded] == [
        ('turn:1', 'legacy.docx', 'write_file'),
    ]


def test_backfill_missing_manifest_records_writes_empty_decision_when_no_artifacts(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    session = _bclass_session(
        'bclass10',
        workspace=workspace,
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'hello', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'hi'},
        ],
    )

    result = store.backfill_missing_manifest_records(session, db_path=db_path)

    assert result == {'source': 'backfill', 'written': 1, 'skipped': 0, 'turns': 1}
    assert store.load_manifest_records(session, db_path=db_path) == []
    assert store.load_manifest_decided_turn_keys(session, db_path=db_path) == {'turn:1'}
