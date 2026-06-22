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
