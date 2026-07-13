import json

from api import models


def test_last_error_at_round_trips_through_metadata_load(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    session = models.Session(session_id="status-roundtrip", last_error_at=123.5)
    session.messages = [{"role": "assistant", "content": "failed", "timestamp": 123}]
    session.save(skip_index=True)

    raw = json.loads(session.path.read_text(encoding="utf-8"))
    assert raw["last_error_at"] == 123.5
    loaded = models.Session.load_metadata_only(session.session_id)
    assert loaded is not None
    assert loaded.last_error_at == 123.5
    assert loaded.compact()["last_error_at"] == 123.5


def test_invalid_last_error_at_is_empty():
    assert models.Session(last_error_at="invalid").last_error_at is None
    assert models.Session(last_error_at=-1).last_error_at is None
