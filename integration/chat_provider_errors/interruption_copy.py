"""Chinese user-visible copy for interrupted-turn recovery markers.

When a live response stream stops mid-turn (process crash, broken SSE,
lost worker bookkeeping, etc.) the WebUI persists an interrupted marker
into ``session.messages``. This module owns the Chinese wording so the
upstream ``api/models.py`` stays a thin re-export seam.

Mirrors the structure of ``messages.py`` (apperror copy table) and is
imported by ``api/models.py`` alongside the existing
``chat_provider_errors`` package.
"""
from __future__ import annotations

# ── Interrupted-turn marker wording ─────────────────────────────────────────
INTERRUPTED_RECOVERED_ZH = '**响应已中断。**\n\n本次回复的实时流在完成前中断。上方部分输出已从运行日志恢复。'
INTERRUPTED_NO_OUTPUT_ZH = '**响应已中断。**\n\n本次回复的实时流在完成前中断。上方的用户消息已保留。'
INTERRUPTED_PENDING_RETRY_ZH = '**响应已中断。**\n\n本次回复的实时流在完成前中断。正在从运行日志恢复部分输出——刷新会话以重试。'
INTERRUPTED_NEUTRAL_ZH = '**响应已中断。**\n\n本次回复的实时流在完成前中断。部分输出可能已丢失。'

OUTCOME_RECOVERED_ZH = '上方部分输出已从运行日志恢复。'
OUTCOME_PENDING_RETRY_ZH = '正在从运行日志恢复部分输出——刷新会话以重试。'
OUTCOME_NO_OUTPUT_ZH = '上方的用户消息已保留。'

MARKER_PREFIX_ZH = '**响应已中断。**\n\n'
MARKER_LEAD_ZH = '本次回复的实时流在完成前中断。'

# ── Interruption cause Chinese translations ─────────────────────────────────

INTERRUPTION_CAUSE_ZH = {
    'process_restart': '服务进程已重启，中断的流式输出无法继续。',
    'stream_run_split_brain': '流式输出已被其他请求覆盖，当前输出已中断。',
    'lost_worker_bookkeeping': '后台 worker 记录丢失，流式输出无法继续。',
    'unknown': '无法确定中断原因。',
}


def build_interrupted_content_zh(
    *,
    recovered_output: bool,
    pending_retry: bool,
    interruption_cause: str = '',  # historically appended to content; kept for API compat
) -> str:
    """Assemble the Chinese interrupted-marker content string.

    ``content`` carries brief status (recovered / pending retry / no output).
    ``interruption_cause`` carries the detailed reason in Chinese in the
    marker dict; it is not appended to the visible content.
    """
    if recovered_output:
        outcome = OUTCOME_RECOVERED_ZH
    elif pending_retry:
        outcome = OUTCOME_PENDING_RETRY_ZH
    else:
        outcome = OUTCOME_NO_OUTPUT_ZH
    return (
        f'{MARKER_PREFIX_ZH}'
        f'{MARKER_LEAD_ZH}'
        f'{outcome}'
    )


# ── Run-journal stale-interrupted recovery control message ─────────────────
# Emitted by ``api/run_journal.py::stale_interrupted_event`` as an apperror
# payload. The frontend recognises this as a recovery-control signal via
# the ``recovery_control: True`` flag (not text matching), but the text is
# still shown briefly in a toast.

RECOVERY_CONTROL_MESSAGE_ZH = '实时 worker 在本次运行完成前已停止。'
RECOVERY_CONTROL_HINT_ZH = (
    '会话记录已恢复到最后一条日志事件。如仍需继续该任务，请新开一轮。'
)
