import importlib.util
import sqlite3
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts' / 'rebind_manifest_turn.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('rebind_manifest_turn', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_store(path: Path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        INSERT INTO session_manifest_records (
          session_id, lineage_key, profile, turn_key, record_kind,
          path, preview, source_tool, created_at, updated_at
        ) VALUES ('session-1', 'lineage-1', 'ops', 'turn:6', 'artifact',
                  'recolor.py', 'file', 'write_file', 1.0, 1.0)
        """
    )
    conn.commit()
    conn.close()


def test_rebind_manifest_turn_is_dry_run_by_default(tmp_path):
    module = _load_module()
    db_path = tmp_path / 'session_manifest.db'
    _create_store(db_path)

    report = module.rebind_manifest_turn(
        db_path,
        lineage_key='lineage-1',
        profile='ops',
        old_key='turn:6',
        new_key='turn:7',
    )

    assert report['status'] == 'ready'
    assert report['dry_run'] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT turn_key FROM session_manifest_records"
        ).fetchall() == [('turn:6',)]


def test_rebind_manifest_turn_applies_explicit_mapping_atomically(tmp_path):
    module = _load_module()
    db_path = tmp_path / 'session_manifest.db'
    _create_store(db_path)

    report = module.rebind_manifest_turn(
        db_path,
        lineage_key='lineage-1',
        profile='ops',
        old_key='turn:6',
        new_key='turn:7',
        apply=True,
    )

    assert report['status'] == 'applied'
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT turn_key, path FROM session_manifest_records"
        ).fetchall() == [('turn:7', 'recolor.py')]


def test_rebind_manifest_turn_rejects_non_mapping(tmp_path):
    module = _load_module()
    db_path = tmp_path / 'session_manifest.db'
    _create_store(db_path)

    with pytest.raises(ValueError, match='must differ'):
        module.rebind_manifest_turn(
            db_path,
            lineage_key='lineage-1',
            profile='ops',
            old_key='turn:6',
            new_key='turn:6',
            apply=True,
        )


def test_rebind_manifest_turn_moves_multiple_records_without_duplicates(tmp_path):
    module = _load_module()
    db_path = tmp_path / 'session_manifest.db'
    _create_store(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO session_manifest_records (
              session_id, lineage_key, profile, turn_key, record_kind,
              path, preview, source_tool, created_at, updated_at
            ) VALUES ('session-1', 'lineage-1', 'ops', 'turn:6', 'artifact',
                      'report.html', 'file', 'terminal', 2.0, 2.0)
            """
        )
        conn.execute(
            """
            INSERT INTO session_manifest_records (
              session_id, lineage_key, profile, turn_key, record_kind,
              path, preview, source_tool, created_at, updated_at
            ) VALUES ('session-1', 'lineage-1', 'ops', 'turn:7', 'artifact',
                      'recolor.py', 'file', 'legacy', 3.0, 3.0)
            """
        )

    report = module.rebind_manifest_turn(
        db_path,
        lineage_key='lineage-1',
        profile='ops',
        old_key='turn:6',
        new_key='turn:7',
        apply=True,
    )

    assert report['status'] == 'applied'
    assert report['record_count'] == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT turn_key, path, source_tool FROM session_manifest_records ORDER BY path"
        ).fetchall() == [
            ('turn:7', 'recolor.py', 'write_file'),
            ('turn:7', 'report.html', 'terminal'),
        ]


def test_rebind_manifest_turn_rolls_back_when_delete_fails(tmp_path):
    module = _load_module()
    db_path = tmp_path / 'session_manifest.db'
    _create_store(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_old_turn_delete
            BEFORE DELETE ON session_manifest_records
            WHEN OLD.turn_key = 'turn:6'
            BEGIN
              SELECT RAISE(ABORT, 'forced rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match='forced rollback'):
        module.rebind_manifest_turn(
            db_path,
            lineage_key='lineage-1',
            profile='ops',
            old_key='turn:6',
            new_key='turn:7',
            apply=True,
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT turn_key, path FROM session_manifest_records"
        ).fetchall() == [('turn:6', 'recolor.py')]
