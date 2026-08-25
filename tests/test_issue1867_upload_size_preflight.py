from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
CONFIG_PY = ROOT / "api" / "config.py"
UPLOAD_PY = ROOT / "api" / "upload.py"


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    signature_end = src.index(")", start)
    brace = src.index("{", signature_end)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : idx + 1]
    raise AssertionError(f"{name} function body not found")


def test_archive_and_transcription_limit_constant_remains_configurable():
    """The retained multipart limit remains available to capped endpoints."""
    config = CONFIG_PY.read_text(encoding="utf-8")

    assert 'MAX_UPLOAD_BYTES = _env_mb_bytes("HERMES_WEBUI_MAX_UPLOAD_MB", 50)' in config


def test_file_picker_does_not_apply_a_local_upload_size_limit():
    """Chat/workspace uploads leave file-size enforcement to outer layers."""
    src = UI_JS.read_text(encoding="utf-8")
    body = _function_body(src, "addFiles")

    assert "MAX_UPLOAD_BYTES" not in body
    assert "S.pendingFiles.push(f)" in body


def test_pending_uploads_do_not_skip_fetch_for_file_size():
    """Queued files are sent without a browser-side size rejection."""
    src = UI_JS.read_text(encoding="utf-8")
    body = _function_body(src, "uploadPendingFiles")

    form_data = body.index("const fd=new FormData()")
    upload_fetch = body.index("fetch(url")

    assert form_data < upload_fetch
    assert "MAX_UPLOAD_BYTES" not in body


def test_upload_size_message_is_not_used_by_unlimited_upload_flow():
    """The old browser-side size rejection helper is no longer in the flow."""
    ui = UI_JS.read_text(encoding="utf-8")
    assert "_uploadTooLargeMessage" not in ui


def test_archive_extraction_limit_tracks_upload_limit():
    """Archive extraction guard should scale with the configured upload limit."""
    upload = UPLOAD_PY.read_text(encoding="utf-8")

    assert "_MAX_EXTRACTED_BYTES = 10 * MAX_UPLOAD_BYTES" in upload
