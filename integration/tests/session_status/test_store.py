import sqlite3

from integration.session_status import store


def test_read_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.db"
    assert store.get_read_until_map([{"profile": "ops", "session_id": "s1"}], db_path=path) == {}
    assert not path.exists()


def test_cursor_is_profile_scoped_and_never_moves_backwards(tmp_path):
    path = tmp_path / "status.db"
    assert store.advance_read_until("ops", "same", 20, db_path=path) == 20
    assert store.advance_read_until("ops", "same", 10, db_path=path) == 20
    assert store.advance_read_until("qa", "same", 7, db_path=path) == 7

    rows = [
        {"profile": "ops", "session_id": "same"},
        {"profile": "qa", "session_id": "same"},
    ]
    assert store.get_read_until_map(rows, db_path=path) == {
        ("ops", "same"): 20,
        ("qa", "same"): 7,
    }


def test_schema_is_minimal_and_idempotent(tmp_path):
    path = tmp_path / "status.db"
    store.advance_read_until("default", "s1", 1, db_path=path)
    store.advance_read_until("default", "s1", 2, db_path=path)
    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(session_read_state)")]
    assert columns == ["profile", "session_id", "read_until"]


def test_delete_profile_only_removes_its_rows(tmp_path):
    path = tmp_path / "status.db"
    store.advance_read_until("ops", "s1", 1, db_path=path)
    store.advance_read_until("qa", "s1", 2, db_path=path)
    store.delete_profile_read_state("ops", db_path=path)
    rows = [{"profile": "ops", "session_id": "s1"}, {"profile": "qa", "session_id": "s1"}]
    assert store.get_read_until_map(rows, db_path=path) == {("qa", "s1"): 2}
