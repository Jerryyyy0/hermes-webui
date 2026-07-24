"""Approval display-localization and scope regression tests."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_approval_card_prefers_display_copy_and_hides_protocol_keys():
    start = MESSAGES_JS.index("function showApprovalCard(")
    end = MESSAGES_JS.index("\nfunction _syncApprovalCollapseButton", start)
    body = MESSAGES_JS[start:end]

    assert "pending.display_description_zh || pending.description" in body
    assert "pattern_keys ||" not in body
    assert 'keys.join(", ")' not in body


def test_gateway_runs_event_adds_chinese_display_copy_without_changing_keys():
    from api.gateway_chat import _gateway_runs_approval_event

    payload = {
        "event": "approval.request",
        "command": "rm -rf /tmp/demo",
        "description": "recursive delete",
        "pattern_key": "recursive delete",
        "pattern_keys": ["recursive delete"],
        "choices": ["once", "session", "always", "deny"],
    }

    event = _gateway_runs_approval_event(payload)

    assert event["display_description_zh"] == "递归删除文件"
    assert event["description"] == "recursive delete"
    assert event["pattern_keys"] == ["recursive delete"]


def test_gateway_mirror_preserves_localized_display_copy():
    from api import route_approvals as approvals
    from api import routes

    sid = "approval-localization-mirror"
    entry = type("Entry", (), {"data": {
        "description": "recursive delete",
        "pattern_key": "recursive delete",
        "pattern_keys": ["recursive delete"],
    }})()

    with routes._lock:
        routes._pending.pop(sid, None)
        routes._gateway_queues[sid] = [entry]
    try:
        approvals.submit_gateway_pending_mirror(sid, entry.data)
        with routes._lock:
            mirror = routes._pending[sid][0]
        assert mirror["display_description_zh"] == "递归删除文件"
        assert mirror["pattern_keys"] == ["recursive delete"]
    finally:
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


def test_tirith_only_always_never_persists_permanently(monkeypatch):
    from api import routes as routes

    sid = "approval-localization-tirith-only"
    calls = []
    pending = {
        "approval_id": "approval-localization-id",
        "pattern_key": "tirith:pipe-to-interpreter",
        "pattern_keys": ["tirith:pipe-to-interpreter"],
        "allow_permanent": False,
    }

    monkeypatch.setattr(routes, "approve_session", lambda _sid, key: calls.append(("session", key)))
    monkeypatch.setattr(routes, "approve_permanent", lambda key: calls.append(("permanent", key)))
    monkeypatch.setattr(routes, "save_permanent_allowlist", lambda _keys: calls.append(("save", None)))
    monkeypatch.setattr(routes, "resolve_gateway_approval", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(routes, "_approval_sse_notify_locked", lambda *_args, **_kwargs: None)

    with routes._lock:
        routes._pending[sid] = [pending]
    try:
        assert routes._resolve_approval_legacy(sid, pending["approval_id"], "always")
    finally:
        with routes._lock:
            routes._pending.pop(sid, None)

    assert calls == [("session", "tirith:pipe-to-interpreter")]
