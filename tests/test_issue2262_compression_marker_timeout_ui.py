from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_preserved_task_list_marker_only_helper_is_strict():
    src = _read("static/ui.js")

    assert "function _isPreservedCompressionTaskListMarkerOnlyText" in src
    start = src.find("function _isPreservedCompressionTaskListMarkerOnlyText")
    end = src.find("function _isPreservedCompressionTaskListMessage", start)
    helper = src[start:end]

    assert "_isPreservedCompressionTaskListMarkerText(text)" in helper
    assert ".replace(/^\\s*\\[your active task list was preserved across context compression\\]" in helper
    assert ".trim()" in helper


def test_marker_only_assistant_message_renders_as_error_not_model_text():
    src = _read("static/ui.js")

    assert "function _isMarkerOnlyAssistantCompressionMessage" in src
    assert "m.role!=='assistant'" in src
    assert "_isPreservedCompressionTaskListMarkerOnlyText(text)" in src
    assert "if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m))" in src
    assert "content='**错误：** 压缩后未收到模型响应，请重试。'" in src


def test_done_and_restore_replace_marker_only_assistant_with_error_toast():
    src = _read("static/messages.js")

    assert "function _replaceMarkerOnlyAssistantWithStreamError(messages)" in src
    assert "_isMarkerOnlyAssistantMessage(msg)" in src
    assert "msg.content='**错误：** 压缩后未收到模型响应，请重试。'" in src
    assert "内部保留的任务列表压缩标记" in src
    assert "_markerOnlyAssistantError=_replaceMarkerOnlyAssistantWithStreamError(S.messages)" in src
    assert "showToast('压缩后未收到模型响应，请重试。',5000,'error')" in src
