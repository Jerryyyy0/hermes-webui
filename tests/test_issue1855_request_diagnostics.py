import json
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

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


class _TimingHandler:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.timing_lines = []

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def _safe_webui_print(self, message):
        self.timing_lines.append(message)


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


def test_request_diagnostics_stage_summary_reports_each_completed_stage():
    diag = RequestDiagnostics(
        "GET",
        "/api/session",
        timeout_seconds=0,
        auto_start=False,
    )
    diag.stage("session.resolve")
    diag.stage("session.message_source")

    summary = diag.stage_summary()

    assert "start=" in summary
    assert "session.resolve=" in summary
    assert "session.message_source=" in summary


def test_session_route_emits_detailed_timing_when_enabled(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "timing-session"
    session = Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hello", "timestamp": 1.0}],
    )
    handler = _TimingHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=50&turn_align=1"
    )
    monkeypatch.setenv("HERMES_DEBUG_SESSION_TIMING", "1")
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])

    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    assert len(handler.timing_lines) == 1
    line = handler.timing_lines[0]
    assert line.startswith("[SESSION_TIMING] session_id=timing-session")
    for field in (
        "session_resolve=",
        "message_source=",
        "stages=start=",
        "session.resolve=",
        "session.message_source=",
        "session.message_projection=",
        "session.response_write=",
    ):
        assert field in line


def test_server_defaults_session_timing_to_disabled():
    source = Path("server.py").read_text(encoding="utf-8")

    assert '"HERMES_DEBUG_SESSION_TIMING", "0"' in source


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
        "session.resolve",
        "session.message_source",
        "session.model_resolve",
        "session.message_projection",
        "session.redact",
        "session.response_write",
        "read_body",
        "resolve_model_provider",
        "session_lock_wait",
        "save_pending_state",
        "stream_registration",
        "worker_thread_start",
        "response_write",
    ):
        assert stage in src
