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
# These four constants are the canonical Chinese versions of the old English
# ``_INTERRUPTED_*_WORDING`` strings in ``api/models.py``. ``api/models.py``
# re-exports them under the legacy ``_INTERRUPTED_*_WORDING`` names so tests
# and callers keep working.

INTERRUPTED_RECOVERED_ZH = (
    '**响应已中断。**\n\n'
    '本次回复的实时流在完成前中断。'
    '上方部分输出已从运行日志恢复，但中断的进程无法继续。'
)
INTERRUPTED_NO_OUTPUT_ZH = (
    '**响应已中断。**\n\n'
    '本次回复的实时流在完成前中断。'
    '上方的用户消息已保留，但未恢复出任何助手输出。'
)
INTERRUPTED_PENDING_RETRY_ZH = (
    '**响应已中断。**\n\n'
    '本次回复的实时流在完成前中断。'
    '正在从运行日志恢复部分输出——刷新会话以重试。'
)
# Neutral wording used when the lazy retry path gives up (max attempts reached
# or the marker has been pending longer than _JOURNAL_RETRY_GIVEUP_SECONDS).
INTERRUPTED_NEUTRAL_ZH = (
    '**响应已中断。**\n\n'
    '本次回复的实时流在完成前中断。'
    '部分输出可能已丢失。'
)

# ── Interruption cause detail strings ───────────────────────────────────────
# Keyed by the ``interruption_cause`` value produced by
# ``_classify_interruption_cause`` in ``api/models.py``.

INTERRUPTION_CAUSE_DETAILS_ZH: dict[str, str] = {
    'process_restart': (
        '迹象：WebUI 进程在本轮开始之后才启动，'
        '因此这看起来是一次真实的进程崩溃或重启。'
    ),
    'stream_run_split_brain': (
        '迹象：浏览器响应流已消失，但 worker 注册表中仍记录着该运行。'
        '这是流与运行记录之间的脑裂。'
    ),
    'lost_worker_bookkeeping': (
        '迹象：响应流已消失，worker 记录中已无对应的活跃运行。'
        '这通常意味着 worker 状态已丢失或在未发出终止事件的情况下被清理。'
    ),
    'unknown': (
        '迹象：响应流已停止，但 WebUI 无法更精确地归类此次中断。'
    ),
}

# ── Outcome fragments used by the dynamic marker builder ────────────────────

OUTCOME_RECOVERED_ZH = (
    '上方部分输出已从运行日志恢复，但中断的进程无法继续。'
)
OUTCOME_PENDING_RETRY_ZH = (
    '正在从运行日志恢复部分输出——刷新会话以重试。'
)
OUTCOME_NO_OUTPUT_ZH = '上方的用户消息已保留，但未恢复出任何助手输出。'

# ── Marker prefix / lead sentence ───────────────────────────────────────────

MARKER_PREFIX_ZH = '**响应已中断。**\n\n'
MARKER_LEAD_ZH = '本次回复的实时流在完成前中断。'


def build_interrupted_content_zh(
    *,
    recovered_output: bool,
    pending_retry: bool,
    interruption_cause: str,
) -> str:
    """Assemble the Chinese interrupted-marker content string.

    Mirrors the logic of ``_interrupted_content_for`` in ``api/models.py``;
    that function now delegates here.
    """
    if recovered_output:
        outcome = OUTCOME_RECOVERED_ZH
    elif pending_retry:
        outcome = OUTCOME_PENDING_RETRY_ZH
    else:
        outcome = OUTCOME_NO_OUTPUT_ZH
    cause_detail = INTERRUPTION_CAUSE_DETAILS_ZH.get(
        interruption_cause,
        INTERRUPTION_CAUSE_DETAILS_ZH['unknown'],
    )
    return (
        f'{MARKER_PREFIX_ZH}'
        f'{MARKER_LEAD_ZH}'
        f'{cause_detail} {outcome}'
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
