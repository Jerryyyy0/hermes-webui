"""Global serial generation queue for common tasks.

Mirrors ``integration.assistant_bubbles.generation``: a single background worker
fed by a Condition-gated pending dict, keyed by profile. Two job kinds:
``seed`` (first-time LLM-generated fallback) and ``mine`` (cluster recent user
questions into the top-3 common tasks).
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from integration.common_tasks import collectors, store
from integration.project_logging import console_info, console_warning, format_kv, with_timestamp

logger = logging.getLogger(__name__)

SEED_SYSTEM_PROMPT = """你是岗位助理常办任务规划器。
根据助理人设与技能,生成 3 个常办任务卡片。
必须遵守:
1. 只输出简体中文。
2. 只输出一个 JSON 数组,长度必须为 3,每个元素含 title/description/trigger_language 字段。
3. title ≤ 15 字;description ≤ 50 字;trigger_language 是用户发起该任务的自然话术,≤ 30 字。
4. 任务必须贴合助理人设与技能,不得编造能力。
5. 3 个任务必须明显不同,覆盖助理最常被请求的工作类型。
6. 不得输出 Markdown、代码块、标题、编号、解释或前后缀。"""

CLUSTER_SYSTEM_PROMPT = """你是用户问题聚类器。
将用户最近提出的问题按语义聚类,输出常办任务候选。
必须遵守:
1. 只输出简体中文(title/description/trigger_language)。
2. 只输出一个 JSON 数组,每个元素含 title/description/trigger_language/members/count 字段。
3. title ≤ 15 字;description ≤ 50 字;trigger_language ≤ 30 字;members 是归入该簇的原始问题文本列表,最多保留 5 个最具代表性的成员(若簇内成员多于 5 个,挑选最典型的 5 条);count 是 members 数组的长度。
4. 同一问题只能归入一个簇;语义相近的应合并(如「写周报」「生成周报」「本周报」归一类)。
5. 单条独成簇的也要输出(count=1);不得编造问题中没有的能力。
6. 不得输出 Markdown、代码块、标题、编号、解释或前后缀。"""

SUCCESS_COOLDOWN_SECONDS = 600
FAILURE_RETRY_SECONDS = 3
MIN_QUESTIONS_FOR_MINING = 5
TOP_N = 3
USER_PROMPT_LOG_MAX_CHARS = 2000
MAX_QUESTION_CHARS = 50
MAX_MEMBERS_PER_CLUSTER = 5


def _log_line(event: str, fields: dict[str, Any] | None = None) -> str:
    suffix = format_kv(fields or {})
    return with_timestamp(f"[webui][common_tasks][{event}]" + (f" {suffix}" if suffix else ""))


def _emit_info(event: str, fields: dict[str, Any] | None = None) -> None:
    console_info(_log_line(event, fields))


def _emit_warning(event: str, fields: dict[str, Any] | None = None) -> None:
    console_warning(_log_line(event, fields))


def _truncate_for_log(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n...(truncated, total {len(text)} chars)"


def _emit_full_content_log(
    event: str,
    meta_fields: dict[str, Any] | None = None,
    *,
    content_fields: dict[str, str] | None = None,
    level: str = "info",
) -> None:
    meta = format_kv(meta_fields or {})
    header = with_timestamp(
        " ".join(part for part in (f"[webui][common_tasks][{event}]", meta) if part)
    )
    sections: list[str] = [header]
    for key, value in (content_fields or {}).items():
        text = str(value or "").rstrip()
        if not text:
            continue
        sections.append(f"----- {key} -----")
        sections.append(text)
    sections.append("----- end -----")
    block = "\n".join(sections)
    if level == "warning":
        console_warning(block)
    else:
        console_info(block)


@dataclass(frozen=True)
class CommonTaskJob:
    profile: str
    profile_path: str
    kind: str  # 'seed' | 'mine'
    fingerprint: str
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


_lock = threading.Lock()
_cv = threading.Condition(_lock)
_pending: dict[str, CommonTaskJob] = {}
_worker_started = False
_disabled = False


def disable_worker_for_tests(value: bool = True) -> None:
    global _disabled
    with _lock:
        _disabled = value


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started or _disabled:
            return
        _worker_started = True
        threading.Thread(target=_worker_loop, name="common-tasks-generator", daemon=True).start()


def enqueue(profile: str, profile_path: Path, kind: str, fingerprint: str = "") -> None:
    if kind not in ("seed", "mine"):
        return
    route = collectors.model_route(profile_path)
    if not route.get("model"):
        _emit_warning(
            "enqueue_model_missing",
            {
                "profile": profile,
                "kind": kind,
                "profile_path": str(profile_path),
                "config_yaml_exists": (Path(profile_path) / "config.yaml").is_file(),
                "provider": route.get("provider"),
                "model": route.get("model"),
            },
        )
    task = CommonTaskJob(
        profile=profile,
        profile_path=str(profile_path),
        kind=kind,
        fingerprint=fingerprint,
        provider=route.get("provider"),
        model=route.get("model"),
        base_url=route.get("base_url"),
    )
    _ensure_worker()
    with _cv:
        _pending[profile] = task
        _cv.notify()


def enqueue_missing_or_stale(profile: str, profile_path: Path) -> None:
    """Decide whether to enqueue a seed or mine job for this profile.

    Mirrors ``assistant_bubbles.generation.should_generate`` gating: a
    ``retry_after`` in the future blocks both seed and mine enqueues, so a
    failing LLM is not hammered on every API call. Seed is re-enqueued as
    long as ``seed_generated_at`` is unset (i.e. real seed never succeeded,
    even if fallback placeholder rows are present).
    """
    _tasks, state = store.read_all(profile_path)
    now = time.time()
    retry_after = _float(state.get("retry_after"))
    if retry_after and retry_after > now:
        return
    if not state.get("seed_generated_at"):
        enqueue(profile, profile_path, "seed")
        return
    questions = collectors.collect_recent_user_questions(profile)
    dedup_count = len({q["text"] for q in questions})
    if dedup_count < MIN_QUESTIONS_FOR_MINING:
        return
    fp = collectors.fingerprint_for_cluster(questions)
    if _should_mine(fp, state):
        enqueue(profile, profile_path, "mine", fp)


def start_pregeneration() -> None:
    """Scan all profiles on server startup and enqueue missing/stale jobs.

    Mirrors ``integration.assistant_bubbles.generation.start_pregeneration``.
    Runs the scan in a daemon thread so server boot is not blocked; each
    profile's enqueue decision happens inline (cheap: just reads db + scans
    session JSONs).
    """

    def _scan() -> None:
        try:
            from api.profiles import list_profiles_api

            for row in list_profiles_api():
                profile = str(row.get("name") or "").strip()
                path = row.get("path")
                if profile and path:
                    enqueue_missing_or_stale(profile, Path(path))
        except Exception:
            logger.debug(_log_line("pregeneration_scan_failed"), exc_info=True)

    _ensure_worker()
    threading.Thread(target=_scan, name="common-tasks-pregeneration", daemon=True).start()


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _should_mine(fp: str, state: dict[str, str]) -> bool:
    now = time.time()
    retry_after = _float(state.get("retry_after"))
    if retry_after and retry_after > now:
        return False
    last_fp = state.get("last_fingerprint") or ""
    last_success = _float(state.get("last_success_at"))
    last_attempt = _float(state.get("last_attempt_at"))
    if last_fp == fp and last_success and now - last_success < SUCCESS_COOLDOWN_SECONDS:
        return False
    if last_fp != fp and last_attempt and now - last_attempt < FAILURE_RETRY_SECONDS:
        return False
    return True


def _worker_loop() -> None:
    while True:
        with _cv:
            while not _pending:
                _cv.wait()
            key = next(iter(_pending))
            task = _pending.pop(key)
        try:
            _run_task(task)
        except Exception:
            logger.debug(
                _log_line(
                    "task_failed",
                    {
                        "profile": task.profile,
                        "kind": task.kind,
                        "fingerprint": task.fingerprint[:12],
                    },
                ),
                exc_info=True,
            )


def _run_task(task: CommonTaskJob) -> None:
    profile_path = Path(task.profile_path)
    if task.kind == "seed":
        _run_seed(task, profile_path)
        return
    if task.kind == "mine":
        _run_mine(task, profile_path)
        return


def _run_seed(task: CommonTaskJob, profile_path: Path) -> None:
    context = collectors.collect_seed_context(task.profile, profile_path)
    if not task.model:
        store.write_seed_tasks(profile_path, _fallback_seed_tasks(context))
        _emit_info(
            "seed_succeeded",
            {
                "profile": task.profile,
                "reason": "missing_profile_default_model_fallback",
                "provider": task.provider,
                "model": task.model,
            },
        )
        return
    user_prompt = collectors.render_seed_user_prompt(context)
    system_prompt = SEED_SYSTEM_PROMPT
    _emit_full_content_log(
        "seed_model_prompt",
        {"profile": task.profile, "provider": task.provider, "model": task.model},
        content_fields={
            "system_prompt": system_prompt,
            "user_prompt": _truncate_for_log(user_prompt, USER_PROMPT_LOG_MAX_CHARS),
        },
    )
    content, error = _call_llm(
        task=task,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=600,
        temperature=0.3,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if content is None:
        _write_seed_failure(task, profile_path, context, reason=str(error))
        return
    _emit_full_content_log(
        "seed_model_response",
        {"profile": task.profile, "provider": task.provider, "model": task.model},
        content_fields={"model_response": str(content or "")},
    )
    tasks, reason = validate_seed_response(content)
    if tasks is None:
        _write_seed_failure(task, profile_path, context, reason=f"seed_{reason}")
        return
    store.write_seed_tasks(profile_path, tasks)
    _emit_info(
        "seed_succeeded",
        {
            "profile": task.profile,
            "reason": "model_generated",
            "provider": task.provider,
            "model": task.model,
            "count": len(tasks),
        },
    )


def _write_seed_failure(
    task: CommonTaskJob,
    profile_path: Path,
    context: dict[str, Any],
    *,
    reason: str,
) -> None:
    """Mirror ``assistant_bubbles.generation._write_failure`` for seed.

    Seed has no "previous success" to preserve (seed only runs while
    ``seed_generated_at`` is unset), so we always write fallback placeholder
    + set ``retry_after`` so LLM is retried after the cooldown.
    """
    now = time.time()
    retry_after = now + FAILURE_RETRY_SECONDS
    fallback = _fallback_seed_tasks(context)
    store.write_seed_placeholder(
        profile_path,
        fallback,
        retry_after=retry_after,
        last_error=str(reason),
    )
    _emit_warning(
        "seed_failed",
        {
            "profile": task.profile,
            "reason": reason,
            "provider": task.provider,
            "model": task.model,
            "cache_action": "fallback_written",
            "retry_after": retry_after,
            "count": len(fallback),
        },
    )


def _run_mine(task: CommonTaskJob, profile_path: Path) -> None:
    questions = collectors.collect_recent_user_questions(task.profile)
    current_fp = collectors.fingerprint_for_cluster(questions)
    if current_fp != task.fingerprint:
        # Question set changed between enqueue and run; re-arm with fresh fingerprint.
        enqueue(task.profile, profile_path, "mine", current_fp)
        return
    if not task.model:
        store.replace_mined_tasks(
            profile_path, [], current_fp, success=False, last_error="missing_model"
        )
        _emit_warning(
            "mine_skipped",
            {"profile": task.profile, "reason": "missing_profile_default_model"},
        )
        return
    if len({q["text"] for q in questions}) < MIN_QUESTIONS_FOR_MINING:
        return
    truncated: list[dict[str, Any]] = []
    for q in questions:
        text = str(q.get("text") or "")
        if len(text) > MAX_QUESTION_CHARS:
            text = text[:MAX_QUESTION_CHARS] + "…"
        truncated.append({**q, "text": text})
    user_prompt = collectors.render_cluster_user_prompt(truncated)
    system_prompt = CLUSTER_SYSTEM_PROMPT
    _emit_full_content_log(
        "mine_model_prompt",
        {
            "profile": task.profile,
            "provider": task.provider,
            "model": task.model,
            "fingerprint": current_fp[:12],
        },
        content_fields={
            "system_prompt": system_prompt,
            "user_prompt": _truncate_for_log(user_prompt, USER_PROMPT_LOG_MAX_CHARS),
        },
    )
    content, error = _call_llm(
        task=task,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=4000,
        temperature=0.2,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if content is None:
        store.replace_mined_tasks(
            profile_path, [], current_fp, success=False, last_error=str(error)[:200]
        )
        _emit_warning(
            "mine_failed",
            {
                "profile": task.profile,
                "reason": str(error),
                "provider": task.provider,
                "model": task.model,
                "fingerprint": current_fp[:12],
            },
        )
        return
    _emit_full_content_log(
        "mine_model_response",
        {
            "profile": task.profile,
            "provider": task.provider,
            "model": task.model,
            "fingerprint": current_fp[:12],
        },
        content_fields={"model_response": str(content or "")},
    )
    original_texts = {q["text"] for q in truncated}
    tasks, reason = validate_cluster_response(content, original_texts)
    if tasks is None:
        store.replace_mined_tasks(
            profile_path, [], current_fp, success=False, last_error=f"cluster_{reason}"[:200]
        )
        _emit_warning(
            "mine_rejected",
            {
                "profile": task.profile,
                "reason": reason,
                "provider": task.provider,
                "model": task.model,
                "fingerprint": current_fp[:12],
            },
        )
        return
    store.replace_mined_tasks(profile_path, tasks, current_fp, success=True)
    _emit_info(
        "mine_succeeded",
        {
            "profile": task.profile,
            "reason": "model_generated",
            "provider": task.provider,
            "model": task.model,
            "fingerprint": current_fp[:12],
            "count": len(tasks),
        },
    )


def _call_llm(
    task: CommonTaskJob,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    extra_body: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    started = time.monotonic()
    try:
        auxiliary_client = importlib.import_module("agent.auxiliary_client")
        call_llm = getattr(auxiliary_client, "call_llm")
        from api.profiles import profile_env_for_background_worker

        with profile_env_for_background_worker(task.profile, purpose="common tasks generation"):
            resp = call_llm(
                task="common_tasks",
                provider=task.provider,
                model=task.model,
                base_url=task.base_url,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=60,
                extra_body=extra_body,
            )
        content = resp.choices[0].message.content
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        _emit_warning(
            "model_call_failed",
            {
                "profile": task.profile,
                "provider": task.provider,
                "model": task.model,
                "elapsed_ms": elapsed_ms,
                "error": exc,
            },
        )
        return None, "model_call_failed"
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    _emit_info(
        "model_call_succeeded",
        {
            "profile": task.profile,
            "provider": task.provider,
            "model": task.model,
            "elapsed_ms": elapsed_ms,
            "output_chars": len(str(content or "")),
        },
    )
    return str(content or ""), "ok"


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text or ""))


def _fallback_seed_tasks(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Static seed tasks used when the profile has no model configured.

    Skill names that are pure-ASCII slugs (e.g. ``claude-code-helper``) are
    skipped to avoid ugly titles like ``用claude-c`` after truncation - mirrors
    the LLM prompt rule that forbids raw English slugs in output.
    """
    name = str(context.get("display_name") or "Hermes").strip() or "Hermes"
    desc = str(context.get("description") or "").strip()
    skills = context.get("skills") or []
    primary_skill = ""
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            label = str(skill.get("label") or skill.get("name") or "").strip()
            if not label:
                continue
            if not _has_cjk(label):
                continue
            if len(label) > 10:
                continue
            primary_skill = label
            break
    if primary_skill:
        return [
            {
                "title": f"用{primary_skill}",
                "description": f"调用 {primary_skill} 协助你处理任务",
                "trigger_language": f"用{primary_skill}帮我处理一下",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
            {
                "title": "概述当前工作",
                "description": f"让 {name} 简要回顾当前工作上下文",
                "trigger_language": "概述一下当前工作",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
            {
                "title": "整理待办",
                "description": "把当前任务梳理成待办清单",
                "trigger_language": "帮我整理一下待办",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
        ]
    if desc:
        return [
            {
                "title": desc[:15],
                "description": f"{name} 协助你 {desc[:40]}",
                "trigger_language": f"帮我{desc[:20]}",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
            {
                "title": "概述当前工作",
                "description": f"让 {name} 简要回顾当前工作上下文",
                "trigger_language": "概述一下当前工作",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
            {
                "title": "整理待办",
                "description": "把当前任务梳理成待办清单",
                "trigger_language": "帮我整理一下待办",
                "query_count": 0,
                "source": "seed",
                "members_json": "[]",
            },
        ]
    return [
        {
            "title": "概述当前工作",
            "description": f"{name} 简要回顾当前工作上下文",
            "trigger_language": "概述一下当前工作",
            "query_count": 0,
            "source": "seed",
            "members_json": "[]",
        },
        {
            "title": "整理待办",
            "description": "把当前任务梳理成待办清单",
            "trigger_language": "帮我整理一下待办",
            "query_count": 0,
            "source": "seed",
            "members_json": "[]",
        },
        {
            "title": "总结对话",
            "description": f"让 {name} 总结本次对话要点",
            "trigger_language": "总结一下我们的对话",
            "query_count": 0,
            "source": "seed",
            "members_json": "[]",
        },
    ]


def _strip_wrapping_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "“", "”", "‘", "’"}:
        return text[1:-1].strip()
    return text


def _parse_json_array(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def validate_seed_response(content: Any) -> tuple[list[dict[str, Any]] | None, str]:
    if not isinstance(content, str):
        return None, "not_string"
    try:
        parsed = _parse_json_array(content)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(parsed, list):
        return None, "not_json_array"
    if len(parsed) != 3:
        return None, "seed_count_not_3"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            return None, "seed_item_not_dict"
        title = _strip_wrapping_quotes(str(item.get("title") or "")).strip()[:15]
        desc = _strip_wrapping_quotes(str(item.get("description") or "")).strip()[:50]
        trig = _strip_wrapping_quotes(str(item.get("trigger_language") or "")).strip()[:30]
        if not title or not trig:
            return None, "seed_missing_field"
        if title in seen:
            return None, "seed_duplicate_title"
        seen.add(title)
        out.append({
            "title": title,
            "description": desc,
            "trigger_language": trig,
            "query_count": 0,
            "source": "seed",
            "members_json": "[]",
        })
    return out, "ok"


def validate_cluster_response(
    content: Any,
    original_questions: set[str],
) -> tuple[list[dict[str, Any]] | None, str]:
    if not isinstance(content, str):
        return None, "not_string"
    try:
        parsed = _parse_json_array(content)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(parsed, list):
        return None, "not_json_array"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = _strip_wrapping_quotes(str(item.get("title") or "")).strip()[:15]
        desc = _strip_wrapping_quotes(str(item.get("description") or "")).strip()[:50]
        trig = _strip_wrapping_quotes(str(item.get("trigger_language") or "")).strip()[:30]
        members = item.get("members") or []
        if not title or not trig or not isinstance(members, list):
            continue
        if not all(isinstance(m, str) for m in members):
            continue
        valid_members = [m for m in members if m in original_questions]
        if len(valid_members) > MAX_MEMBERS_PER_CLUSTER:
            valid_members = valid_members[:MAX_MEMBERS_PER_CLUSTER]
        count = len(valid_members)
        if count < 3:
            continue
        if title in seen:
            continue
        seen.add(title)
        out.append({
            "title": title,
            "description": desc,
            "trigger_language": trig,
            "query_count": count,
            "source": "mined",
            "members_json": json.dumps(valid_members, ensure_ascii=False),
        })
    if not out:
        return None, "no_valid_cluster"
    out.sort(key=lambda x: x["query_count"], reverse=True)
    return out[:TOP_N], "ok"
