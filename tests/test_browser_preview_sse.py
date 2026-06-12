import time
from pathlib import Path

from api.browser_preview import (
    BROWSER_PREVIEW_EVENT,
    BROWSER_PREVIEW_SOURCE,
    BrowserPreviewEmitter,
    browser_preview_payload,
    is_browser_tool_name,
    preview_delay_seconds,
    resolve_camofox_frame_origin,
    resolve_camofox_preview_url,
)


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
GATEWAY_PY = (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")


def test_is_browser_tool_name():
    assert is_browser_tool_name("browser_navigate")
    assert is_browser_tool_name("browser_click")
    assert not is_browser_tool_name("web_search")
    assert not is_browser_tool_name("")


def test_resolve_browser_preview_url_validates_scheme():
    env = {"BROWSER_PREVIEW_URL": "http://192.168.1.139:9377/"}
    assert resolve_camofox_preview_url(env) == "http://192.168.1.139:9377"
    assert resolve_camofox_frame_origin(env) == "http://192.168.1.139:9377"
    assert resolve_camofox_preview_url({"BROWSER_PREVIEW_URL": "ftp://example.com"}) == ""
    assert resolve_camofox_frame_origin({"BROWSER_PREVIEW_URL": "ftp://example.com"}) == ""
    assert resolve_camofox_preview_url({"BROWSER_PREVIEW_URL": ""}) == ""
    assert resolve_camofox_frame_origin({"BROWSER_PREVIEW_URL": ""}) == ""
    assert resolve_camofox_preview_url({"BROWSER_PREVIEW_URL": "not-a-url"}) == ""


def test_resolve_browser_preview_url_preserves_path_and_query():
    env = {
        "BROWSER_PREVIEW_URL": "http://192.168.1.139:6080/vnc.html?path=websockify?token=user1",
    }
    url = resolve_camofox_preview_url(env)
    assert url == "http://192.168.1.139:6080/vnc.html?path=websockify?token=user1"
    assert resolve_camofox_frame_origin(env) == "http://192.168.1.139:6080"


def test_browser_preview_payload_shape():
    env = {"BROWSER_PREVIEW_URL": "https://vnc.example.com:9377"}
    payload = browser_preview_payload("sid-1", "stream-1", "browser_navigate", env)
    assert payload == {
        "session_id": "sid-1",
        "stream_id": "stream-1",
        "url": "https://vnc.example.com:9377",
        "source": BROWSER_PREVIEW_SOURCE,
        "tool": "browser_navigate",
    }
    assert browser_preview_payload("sid-1", "stream-1", "browser_navigate", {}) is None


def test_preview_delay_seconds_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("BROWSER_PREVIEW_DELAY_SECONDS", raising=False)
    assert preview_delay_seconds() == 5.0
    monkeypatch.setenv("BROWSER_PREVIEW_DELAY_SECONDS", "3")
    assert preview_delay_seconds() == 3.0
    assert preview_delay_seconds({"BROWSER_PREVIEW_DELAY_SECONDS": "0"}) == 0.0
    assert preview_delay_seconds({"BROWSER_PREVIEW_DELAY_SECONDS": "bad"}) == 5.0


def test_browser_preview_emitter_emits_once():
    events = []
    put = lambda event, payload: events.append((event, payload))
    emitter = BrowserPreviewEmitter()
    env = {
        "BROWSER_PREVIEW_URL": "http://127.0.0.1:9377",
        "BROWSER_PREVIEW_DELAY_SECONDS": "0",
    }

    assert emitter.maybe_emit(put, "sid", "stream", "browser_navigate", env) is True
    assert emitter.maybe_emit(put, "sid", "stream", "browser_click", env) is False
    assert emitter.maybe_emit(put, "sid", "stream", "web_search", env) is False
    assert events == [
        (
            BROWSER_PREVIEW_EVENT,
            {
                "session_id": "sid",
                "stream_id": "stream",
                "url": "http://127.0.0.1:9377",
                "source": BROWSER_PREVIEW_SOURCE,
                "tool": "browser_navigate",
            },
        )
    ]


def test_browser_preview_emitter_delays_emit():
    events = []
    put = lambda event, payload: events.append((event, payload))
    emitter = BrowserPreviewEmitter()
    env = {
        "BROWSER_PREVIEW_URL": "http://127.0.0.1:9377",
        "BROWSER_PREVIEW_DELAY_SECONDS": "0.15",
    }
    start = time.monotonic()
    assert emitter.maybe_emit(put, "sid", "stream", "browser_navigate", env) is True
    assert events == []
    deadline = start + 1.0
    while not events and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(events) == 1
    assert time.monotonic() - start >= 0.15
    assert events[0][0] == BROWSER_PREVIEW_EVENT


def test_streaming_wires_browser_preview_on_tool_start():
    assert "from api.browser_preview import BrowserPreviewEmitter" in STREAMING_PY
    assert "_maybe_emit_browser_preview(name)" in STREAMING_PY
    on_tool_start = STREAMING_PY.split("def on_tool_start", 1)[1].split("\n            def on_tool_complete", 1)[0]
    assert "_maybe_emit_browser_preview(name)" in on_tool_start
    on_tool = STREAMING_PY.split("def on_tool(*cb_args", 1)[1].split("\n            def on_tool_start", 1)[0]
    assert "_maybe_emit_browser_preview(name)" in on_tool


def test_gateway_wires_browser_preview_on_tool_start():
    assert "from api.browser_preview import BrowserPreviewEmitter" in GATEWAY_PY
    assert "_maybe_emit_gateway_browser_preview" in GATEWAY_PY
    block = GATEWAY_PY.split('if sse_event == "hermes.tool.progress":', 1)[1].split("sse_event = \"message\"", 1)[0]
    assert 'if event_name == "tool":' in block
    assert "_maybe_emit_gateway_browser_preview(event_payload.get(\"name\"))" in block


def test_messages_js_listens_for_browser_preview():
    assert "source.addEventListener('browser_preview'" in MESSAGES_JS
    block = MESSAGES_JS.split("source.addEventListener('browser_preview'", 1)[1].split(
        "source.addEventListener('todo_state'", 1
    )[0]
    assert "openBrowserPreview(url" in block
    assert "window.open(url,'_blank','noopener,noreferrer')" not in block
    assert "'browser_preview'" in MESSAGES_JS.split("for(const _runJournalEventName of [", 1)[1].split("]", 1)[0]


def test_load_dir_preserves_browser_preview():
    block = WORKSPACE_JS.split("async function loadDir", 1)[1].split("\nasync function _refreshGitBadge", 1)[0]
    assert "_isBrowserPreviewOpen()" in block
    assert "preservePreview=!!(opts&&opts.preservePreview)||_isBrowserPreviewOpen()" in block.replace(" ", "")


def test_workspace_js_embeds_browser_preview():
    assert "function openBrowserPreview(url" in WORKSPACE_JS
    assert "previewBrowserIframe" in WORKSPACE_JS
    assert "browser-preview-active" in WORKSPACE_JS


def test_render_file_tree_skips_browser_preview():
    ui_js = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    block = ui_js.split("function renderFileTree(){", 1)[1].split("function _isWorkspaceTreeMoveDrag", 1)[0]
    assert "_previewCurrentMode==='browser'" in block
    assert "_previewBrowserUrl" in block


def test_csp_allows_browser_preview_url_frame_origin(monkeypatch):
    from api.helpers import _build_csp_enforced_policy

    monkeypatch.setenv("BROWSER_PREVIEW_URL", "http://192.168.1.139:9377")
    policy = _build_csp_enforced_policy("")
    assert "frame-src 'self' http://192.168.1.139:9377" in policy


def test_csp_allows_browser_preview_url_with_path_and_query(monkeypatch):
    from api.helpers import _build_csp_enforced_policy

    monkeypatch.setenv(
        "BROWSER_PREVIEW_URL",
        "http://192.168.1.139:6080/vnc.html?path=websockify?token=user1",
    )
    policy = _build_csp_enforced_policy("")
    assert "frame-src 'self' http://192.168.1.139:6080" in policy
