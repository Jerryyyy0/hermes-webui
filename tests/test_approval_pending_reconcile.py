"""Regression tests for orphaned gateway approval mirrors in /api/approval/pending.

When an agent-side approval times out, `_drop_entry()` removes the entry from
the agent's `_gateway_queues` but does not notify the WebUI. The mirrored copy
in `route_approvals._pending[sid]` becomes an orphan that is still served by
`GET /api/approval/pending`, causing a stale approval card in the frontend.

These tests verify that `_handle_approval_pending` reconciles the gateway
mirror before reading `_pending`, so:
  - an orphaned mirror (no live gateway queue entry) is purged -> pending: null
  - a live approval (matching gateway queue entry) is preserved -> pending returned
"""
from __future__ import annotations

import urllib.parse
import uuid
from types import SimpleNamespace

import pytest

from api import route_approvals as ra
from api import routes as r


def _capture_json(monkeypatch):
    """Patch routes.j() to capture the response payload instead of writing."""
    captured = {}

    def fake_j(handler, data, status=200, extra_headers=None):
        captured["payload"] = data
        captured["status"] = status
        return data

    monkeypatch.setattr(r, "j", fake_j)
    return captured


def _make_parsed(sid: str):
    query = urllib.parse.urlencode({"session_id": sid})
    return urllib.parse.urlparse(f"/api/approval/pending?{query}")


def _make_mirror(approval_data: dict, token: str) -> dict:
    """Build a gateway mirror entry matching what submit_gateway_pending_mirror produces."""
    entry = dict(approval_data)
    entry.setdefault("approval_id", f"gwlocal:{token}")
    entry[ra._GATEWAY_MIRROR_FLAG] = True
    entry[ra._GATEWAY_MIRROR_TOKEN] = token
    return entry


class TestApprovalPendingReconcilesGatewayMirror:
    """GET /api/approval/pending must reconcile before returning _pending head."""

    def test_orphaned_mirror_is_purged(self, monkeypatch):
        """A mirror whose agent-side entry has been dropped returns pending: null."""
        sid = f"reconcile-orphan-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        approval_data = {
            "command": "rm -rf /tmp/stale",
            "description": "stale approval",
            "pattern_key": "dangerous_command",
            "pattern_keys": ["dangerous_command"],
        }
        mirror = _make_mirror(approval_data, token)

        # Simulate post-timeout state: mirror exists in _pending but
        # _gateway_queues[sid] is empty (agent's _drop_entry already ran).
        with ra._lock:
            ra._pending[sid] = [mirror]

        captured = _capture_json(monkeypatch)
        r._handle_approval_pending(object(), _make_parsed(sid))

        assert captured["status"] == 200
        assert captured["payload"]["pending"] is None
        assert captured["payload"]["pending_count"] == 0

        # The orphaned mirror must also be removed from _pending itself.
        with ra._lock:
            assert sid not in ra._pending

    def test_live_approval_is_preserved(self, monkeypatch):
        """A mirror backed by a live gateway queue entry is returned normally."""
        sid = f"reconcile-live-{uuid.uuid4().hex[:8]}"
        token = uuid.uuid4().hex
        approval_data = {
            "command": "rm -rf /tmp/live",
            "description": "live approval",
            "pattern_key": "dangerous_command",
            "pattern_keys": ["dangerous_command"],
        }
        # Simulate a live agent-side entry with a pre-stamped mirror token.
        live_entry = SimpleNamespace(data={**approval_data, "_webui_mirror_token": token})
        mirror = _make_mirror(approval_data, token)

        with ra._lock:
            ra._gateway_queues[sid] = [live_entry]
            ra._pending[sid] = [mirror]

        captured = _capture_json(monkeypatch)
        r._handle_approval_pending(object(), _make_parsed(sid))

        assert captured["status"] == 200
        assert captured["payload"]["pending"] is not None
        assert captured["payload"]["pending"]["command"] == approval_data["command"]
        assert captured["payload"]["pending_count"] == 1

        with ra._lock:
            assert sid in ra._pending

    def test_no_pending_is_harmless(self, monkeypatch):
        """Reconcile on an unknown session does not crash."""
        sid = f"reconcile-empty-{uuid.uuid4().hex[:8]}"
        captured = _capture_json(monkeypatch)
        r._handle_approval_pending(object(), _make_parsed(sid))
        assert captured["payload"]["pending"] is None
        assert captured["payload"]["pending_count"] == 0

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        # Clean up any session keys created during tests (best-effort).
        with ra._lock:
            for key in list(ra._pending):
                if key.startswith("reconcile-"):
                    ra._pending.pop(key, None)
            for key in list(ra._gateway_queues):
                if key.startswith("reconcile-"):
                    ra._gateway_queues.pop(key, None)
