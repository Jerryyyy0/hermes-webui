"""Regression checks for session pinning and context menu access."""

import json
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created):
    payload = {
        "title": f"Pin {len(created) + 1}",
        "messages": [{"role": "user", "content": "keep this conversation handy"}],
        "model": "test/pin-cap",
    }
    d, status = post("/api/session/import", payload)
    assert status == 200
    sid = d["session"]["session_id"]
    created.append(sid)
    return sid


def test_session_pin_endpoint_allows_unlimited_pins():
    created = []
    try:
        pinned = [make_session(created) for _ in range(10)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        d, status = post("/api/session/pin", {"session_id": pinned[0], "pinned": False})
        assert status == 200
        assert d["session"]["pinned"] is False
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})


def test_session_pin_endpoint_has_no_quota_guard():
    assert "pinned_sessions_limit" not in ROUTES_PY
    assert "_visible_pinned_lineage_ids" not in ROUTES_PY
    assert "_session_counts_toward_pin_quota" not in ROUTES_PY
    assert "function _pinnedSessionCount()" not in SESSIONS_JS
    assert "function _getPinnedSessionsLimit()" not in SESSIONS_JS
    assert "await api('/api/session/pin'" in SESSIONS_JS
    assert ".session-action-opt.is-disabled{opacity:.55;cursor:not-allowed;}" in STYLE_CSS


def test_session_rows_open_action_menu_from_right_click():
    assert 'el.oncontextmenu=(e)=>{' in SESSIONS_JS
    context_idx = SESSIONS_JS.find('el.oncontextmenu=(e)=>{')
    assert context_idx != -1
    block = SESSIONS_JS[context_idx:SESSIONS_JS.find('};', context_idx) + 2]
    assert 'e.preventDefault();' in block
    assert 'e.stopPropagation();' in block
    assert '_openSessionActionMenu(s, actions||el);' in block
