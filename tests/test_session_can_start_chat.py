"""Tests for GET /api/session/status can_start_chat and _session_can_start_chat."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import api.config as config
import api.routes as routes
import pytest

from tests.conftest import TEST_BASE, _post


class _FakeSession:
    def __init__(self, session_id: str = "sess-can-start"):
        self.session_id = session_id
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_started_at = None


@pytest.fixture(autouse=True)
def _reset_stream_registry():
    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    yield
    with config.STREAMS_LOCK:
        config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()


def test_session_can_start_chat_idle_session():
    session = _FakeSession()
    assert routes._session_can_start_chat(session) is True


def test_session_can_start_chat_blocks_when_stream_in_streams():
    session = _FakeSession()
    stream_id = "stream-live-sse"
    session.active_stream_id = stream_id
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = object()
    assert routes._session_can_start_chat(session) is False


def test_session_can_start_chat_blocks_when_stream_in_active_runs():
    session = _FakeSession()
    stream_id = "stream-worker"
    session.active_stream_id = stream_id
    config.register_active_run(stream_id, session_id=session.session_id, phase="running")
    assert routes._session_can_start_chat(session) is False


def test_session_can_start_chat_blocks_active_run_unwind_without_sidecar_stream_id():
    """Cancel clears active_stream_id before ACTIVE_RUNS unwind (#3808)."""
    session = _FakeSession()
    stream_id = "stream-cancel-unwind"
    session.active_stream_id = None
    config.register_active_run(stream_id, session_id=session.session_id, phase="cancelling")
    assert routes._session_can_start_chat(session) is False


def test_session_can_start_chat_blocks_pending_turn_in_grace():
    session = _FakeSession()
    stream_id = "stream-pending"
    session.active_stream_id = stream_id
    session.pending_user_message = "hello"
    session.pending_started_at = time.time()
    assert routes._session_can_start_chat(session) is False


def test_session_can_start_chat_allows_stale_active_stream_id_not_in_memory():
    session = _FakeSession()
    session.active_stream_id = "stale-stream-gone"
    assert routes._session_can_start_chat(session) is True


class _RouteCaptureHandler:
    command = "GET"
    path = "/"
    client_address = ("127.0.0.1", 12345)


def test_session_status_route_includes_can_start_chat(monkeypatch):
    sid = "sess-status-can-start"
    session = SimpleNamespace(
        session_id=sid,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        _loaded_metadata_only=True,
    )
    captured = {}

    def fake_j(_handler, payload, *_, **kwargs):
        captured["payload"] = payload
        return True

    def fake_get_session(session_id, metadata_only=False):
        assert session_id == sid
        return session

    def fake_session_status(session_id):
        assert session_id == sid
        return {"session_id": session_id, "agent_running": False, "active_stream_id": None}

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "get_session", fake_get_session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _s: False)
    monkeypatch.setattr("api.session_ops.session_status", fake_session_status)

    routes.handle_get(_RouteCaptureHandler(), urlparse(f"/api/session/status?session_id={sid}"))

    assert captured["payload"]["can_start_chat"] is True


def test_status_endpoint_returns_can_start_chat_when_idle(cleanup_test_sessions):
    data = _post(TEST_BASE, "/api/session/new", {})
    sid = data["session"]["session_id"]
    cleanup_test_sessions.append(sid)

    import urllib.request

    req = urllib.request.Request(f"{TEST_BASE}/api/session/status?session_id={sid}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read())

    assert payload["session_id"] == sid
    assert payload["can_start_chat"] is True
