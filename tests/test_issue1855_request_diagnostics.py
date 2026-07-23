import json
import re
from pathlib import Path

import pytest

from integration.project_logging.config import configure_logging
import api.models as models
from api.models import Session
from api.request_diagnostics import RequestDiagnostics


@pytest.fixture(autouse=True)
def _reset_logging_handlers():
    configure_logging(force=True)
    yield

_TS_RE = re.compile(
    r"^(?:(?:INFO|WARNING|ERROR|CRITICAL|DEBUG) )?"
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
)


def _without_ts(line: str) -> str:
    assert _TS_RE.match(line)
    return _TS_RE.sub("", line, count=1)


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(name)


def test_request_diagnostics_timeout_record_includes_stage_without_thread_stacks_by_default(capsys):
    diag = RequestDiagnostics(
        "GET",
        "/api/sessions?all_profiles=1",
        timeout_seconds=5,
        auto_start=False,
    )
    diag.stage("all_sessions.read_index")

    diag._on_timeout()

    line = _without_ts(capsys.readouterr().err.strip())
    assert line.startswith("[webui][slow_request][running] GET /api/sessions")
    assert "current_stage=all_sessions.read_index" in line
    assert "stages=start:" in line
    assert "all_sessions.read_index:" in line
    assert "elapsed=" in line
    assert "thread_stacks=yes" not in line


def test_request_diagnostics_timeout_record_includes_thread_stacks_when_enabled(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "DEBUG")
    configure_logging(force=True)
    diag = RequestDiagnostics(
        "GET",
        "/api/sessions?all_profiles=1",
        timeout_seconds=5,
        auto_start=False,
    )
    diag.stage("all_sessions.read_index")

    diag._on_timeout()

    line = _without_ts(capsys.readouterr().err.strip())
    assert "thread_stacks=yes" in line


def test_request_diagnostics_maybe_start_is_limited_to_issue1855_paths():
    assert RequestDiagnostics.maybe_start("GET", "/api/sessions") is not None
    assert RequestDiagnostics.maybe_start("POST", "/api/chat/start") is not None
    assert RequestDiagnostics.maybe_start("GET", "/health") is None
    assert RequestDiagnostics.maybe_start("POST", "/api/session/new") is None


def test_all_sessions_reports_internal_index_stages(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda sessions: None)
    models.SESSIONS.clear()

    s = Session(
        session_id="issue1855_indexed",
        title="Indexed",
        messages=[{"role": "user", "content": "hi", "timestamp": 100}],
    )
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False), encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "updated_at": s.updated_at,
                    "workspace": s.workspace,
                    "model": s.model,
                    "message_count": 1,
                    "created_at": s.created_at,
                    "pinned": False,
                    "archived": False,
                    "last_message_at": 100,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    diag = _StageRecorder()
    rows = models.all_sessions(diag=diag)

    assert [row["session_id"] for row in rows] == [s.session_id]
    assert "all_sessions.read_index" in diag.stages
    assert "all_sessions.overlay_lock" in diag.stages
    assert "all_sessions.lineage_metadata" in diag.stages


def test_issue1855_target_routes_are_wired_to_diagnostics():
    src = Path("api/routes.py").read_text(encoding="utf-8")

    assert 'RequestDiagnostics.maybe_start("GET", parsed.path' in src
    assert "all_sessions(diag=diag)" in src
    assert 'RequestDiagnostics.maybe_start("POST", parsed.path' in src
    assert "_handle_chat_start(handler, body, diag=diag)" in src
    for stage in (
        "read_body",
        "resolve_model_provider",
        "session_lock_wait",
        "save_pending_state",
        "stream_registration",
        "worker_thread_start",
        "response_write",
    ):
        assert stage in src
