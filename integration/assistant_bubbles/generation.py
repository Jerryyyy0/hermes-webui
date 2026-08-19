"""Global serial generation queue for assistant bubbles."""

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

from integration.assistant_bubbles import collectors, copy, store
from integration.project_logging import console_info, console_warning, format_kv, with_timestamp

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 Profile 助理气泡文案生成器。
你的任务是根据用户提供的 Profile 上下文，生成一条会显示在助理头像旁边的短气泡文案。
必须遵守：
1. 只输出简体中文。
2. 只输出一条短文案，总长度最多 50 个字符（含标点与 emoji）。
3. 句中必须使用逗号、顿号等普通标点做自然断句，禁止把多个分句粘成一串无标点长句。
4. 只基于输入上下文生成，不得编造上下文没有的信息。
5. 不得承诺已经完成、正在执行或将自动执行任何动作。
6. 不得输出 JSON、Markdown、代码块、标题、编号、候选列表、解释或前后缀。
7. 返回内容必须能直接展示给用户。
8. 若使用 emoji：emoji 紧贴语气词之后、语气标点（~、！、？）之前；正确「哟😊！」「哦📝~」，错误「哟！😊」「哦~📝」。此规则只约束 emoji 位置，不影响句中其它逗号、顿号。"""

EMOTION_SYSTEM_PROMPT = """你是 Profile 助理气泡文案生成器。
你的任务是根据用户提供的 Profile 上下文，生成 4 条会显示在助理头像旁边的拟人化情绪短句。
必须遵守：
1. 只输出简体中文。
2. 每条短句最多 50 个字符（含标点与 emoji）；4 条必须明显不同，不能重复或同义改写。
3. 句中应使用逗号、冒号等标点做自然断句，禁止无标点粘连长句。
4. 只基于输入上下文生成，不得编造上下文没有的信息。
5. 不得承诺已经完成、正在执行或将自动执行任何动作。
6. 只输出一个 JSON 数组字符串，数组长度必须为 4，每个元素必须是字符串；不得输出 Markdown、代码块、标题、编号、候选列表、解释或前后缀。
7. 若使用 emoji：emoji 紧贴句末内容之后、语气标点（~、！、？）之前；正确「满格⚡！」，错误「满格！⚡」。此规则只约束 emoji 位置，不影响句中其它逗号。"""

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
GENERATION_ORDER = ("assistant_intro", "memory", "skill", "emotion")
SUCCESS_REGEN_COOLDOWN_SECONDS = 10 * 60
FAILURE_RETRY_SECONDS = 3
EMOTION_REFRESH_SECONDS = 10 * 60
USER_PROMPT_LOG_MAX_CHARS = 500


def _log_line(event: str, fields: dict[str, Any] | None = None) -> str:
    suffix = format_kv(fields or {})
    return with_timestamp(f"[webui][assistant_bubbles][{event}]" + (f" {suffix}" if suffix else ""))


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
    """Emit prompt/response logs as a readable multi-line block (no truncation)."""
    meta = format_kv(meta_fields or {})
    header = with_timestamp(
        " ".join(part for part in (f"[webui][assistant_bubbles][{event}]", meta) if part)
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
class BubbleTask:
    profile: str
    profile_path: str
    category: str
    fingerprint: str
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class BubbleRefreshTask:
    profile: str
    profile_path: str


_lock = threading.Lock()
_cv = threading.Condition(_lock)
_pending: dict[tuple[str, str], BubbleTask] = {}
_pending_refreshes: dict[tuple[str, str], BubbleRefreshTask] = {}
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
        threading.Thread(target=_worker_loop, name="assistant-bubbles-generator", daemon=True).start()


def enqueue(profile: str, profile_path: Path, category: str, fingerprint: str) -> None:
    if category not in GENERATION_ORDER:
        return
    route = collectors.model_route(profile_path)
    task = BubbleTask(
        profile=profile,
        profile_path=str(profile_path),
        category=category,
        fingerprint=fingerprint,
        provider=route.get("provider"),
        model=route.get("model"),
        base_url=route.get("base_url"),
    )
    _ensure_worker()
    with _cv:
        _pending[(profile, category)] = task
        _cv.notify()


def enqueue_missing_or_stale(profile: str, profile_path: Path, cache: dict[str, Any] | None = None) -> None:
    """Schedule stale checking on the generator worker without blocking an HTTP read."""
    del cache  # The worker must re-read the authoritative cache when it handles the request.
    task = BubbleRefreshTask(profile=profile, profile_path=str(profile_path))
    _ensure_worker()
    with _cv:
        if _disabled:
            return
        _pending_refreshes[(profile, str(profile_path))] = task
        _cv.notify()


def _enqueue_missing_or_stale_now(profile: str, profile_path: Path) -> None:
    cache = store.read_store(profile_path)
    for category in GENERATION_ORDER:
        context = collectors.collect_context(profile, profile_path, category)
        fp = collectors.fingerprint_for(category, context)
        if should_generate(category, fp, cache, now=time.time()):
            enqueue(profile, profile_path, category, fp)


def start_pregeneration() -> None:
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
    threading.Thread(target=_scan, name="assistant-bubbles-pregeneration", daemon=True).start()


def should_generate(category: str, fingerprint: str, cache: dict[str, Any] | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if category == "memory":
        # Empty memory contexts use deterministic copy and do not need model calls.
        pass
    if not cache:
        return True
    generation = cache.get("generation") if isinstance(cache, dict) else {}
    entry = generation.get(category) if isinstance(generation, dict) else None
    if not isinstance(entry, dict):
        return True
    retry_after = entry.get("retry_after")
    if isinstance(retry_after, (int, float)) and retry_after > now:
        return False
    generated_at = entry.get("generated_at")
    last_attempt_at = entry.get("last_attempt_at")
    if generated_at is None:
        return True
    if entry.get("fingerprint") != fingerprint:
        if generated_at is None:
            return True
        if isinstance(last_attempt_at, (int, float)) and now - last_attempt_at < SUCCESS_REGEN_COOLDOWN_SECONDS:
            return False
        return True
    if category == "emotion" and isinstance(generated_at, (int, float)):
        if now - generated_at >= EMOTION_REFRESH_SECONDS:
            if isinstance(last_attempt_at, (int, float)) and now - last_attempt_at < SUCCESS_REGEN_COOLDOWN_SECONDS:
                return False
            return True
    return False


def _worker_loop() -> None:
    while True:
        with _cv:
            while not _pending and not _pending_refreshes:
                _cv.wait()
            if _pending:
                key = next(iter(_pending))
                task = _pending.pop(key)
                refresh_task = None
            else:
                key = next(iter(_pending_refreshes))
                refresh_task = _pending_refreshes.pop(key)
                task = None
        try:
            if refresh_task is not None:
                _enqueue_missing_or_stale_now(refresh_task.profile, Path(refresh_task.profile_path))
            elif task is not None:
                _run_task(task)
        except Exception:
            if refresh_task is not None:
                logger.debug(
                    _log_line("refresh_failed", {"profile": refresh_task.profile}),
                    exc_info=True,
                )
            elif task is not None:
                logger.debug(
                    _log_line(
                        "task_failed",
                        {
                            "profile": task.profile,
                            "category": task.category,
                            "fingerprint": task.fingerprint[:12],
                        },
                    ),
                    exc_info=True,
                )


def _run_task(task: BubbleTask) -> None:
    profile_path = Path(task.profile_path)
    cache = store.read_store(profile_path)
    context = collectors.collect_context(task.profile, profile_path, task.category)
    current_fp = collectors.fingerprint_for(task.category, context)
    if current_fp != task.fingerprint:
        enqueue(task.profile, profile_path, task.category, current_fp)
        return
    if not should_generate(task.category, current_fp, cache, now=time.time()):
        return
    if task.category == "memory" and not context.get("latest_memory"):
        _write_success(
            profile_path,
            task.category,
            copy.fallback_text(task.category, context),
            current_fp,
            task=task,
            reason="empty_memory_fallback",
        )
        return
    if task.category == "skill" and not context.get("skills_count"):
        _write_success(
            profile_path,
            task.category,
            copy.fallback_text(task.category, context),
            current_fp,
            task=task,
            reason="empty_skill_fallback",
        )
        return
    if not task.model:
        _write_success(
            profile_path,
            task.category,
            _fallback_for_category(task.category, context),
            current_fp,
            task=task,
            reason="missing_profile_default_model_fallback",
        )
        return
    text_or_texts, failure_reason = _generate_with_model(task, profile_path, context)
    if text_or_texts is None:
        _write_failure(profile_path, task.category, current_fp, reason=failure_reason, task=task, context=context)
        return
    latest_context = collectors.collect_context(task.profile, profile_path, task.category)
    latest_fp = collectors.fingerprint_for(task.category, latest_context)
    if latest_fp != current_fp:
        enqueue(task.profile, profile_path, task.category, latest_fp)
        return
    _write_success(profile_path, task.category, text_or_texts, current_fp, task=task, reason="model_generated")


def _fallback_for_category(category: str, context: dict[str, Any]) -> str | list[str]:
    if category == "emotion":
        return copy.fallback_emotions(context)
    return copy.fallback_text(category, context)


def _generation_entry(fingerprint: str, *, success: bool) -> dict[str, Any]:
    now = time.time()
    return {
        "fingerprint": fingerprint,
        "generated_at": now if success else None,
        "last_attempt_at": now,
        "retry_after": None if success else now + FAILURE_RETRY_SECONDS,
    }


def _output_metadata(text_or_texts: str | list[str]) -> dict[str, int]:
    if isinstance(text_or_texts, list):
        return {
            "output_count": len(text_or_texts),
            "output_chars": sum(len(str(item)) for item in text_or_texts),
        }
    text = str(text_or_texts or "")
    return {"output_chars": len(text)}


def _content_metadata(content: Any) -> dict[str, int]:
    if isinstance(content, list):
        return {
            "output_count": len(content),
            "output_chars": sum(len(str(item)) for item in content),
        }
    text = str(content or "")
    return {"output_chars": len(text)}


def _write_success(
    profile_path: Path,
    category: str,
    text_or_texts: str | list[str],
    fingerprint: str,
    *,
    task: BubbleTask | None = None,
    reason: str = "model_generated",
) -> None:
    entry = _generation_entry(fingerprint, success=True)
    _emit_info(
        "generation_succeeded",
        {
            "profile": task.profile if task else "",
            "category": category,
            "fingerprint": fingerprint[:12],
            "reason": reason,
            "provider": task.provider if task else None,
            "model": task.model if task else None,
            "generated_at": entry.get("generated_at"),
            **_output_metadata(text_or_texts),
        },
    )
    store.update_category(profile_path, category, text_or_texts, entry)


def _category_has_cached_text(cache: dict[str, Any], category: str) -> bool:
    items = cache.get("items") if isinstance(cache, dict) else []
    if not isinstance(items, list):
        return False
    expected = 4 if category == "emotion" else 1
    found = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != category:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            found += 1
    return found >= expected


def _write_failure(
    profile_path: Path,
    category: str,
    fingerprint: str,
    *,
    reason: str = "unknown",
    task: BubbleTask | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    cache = store.read_store(profile_path) or store.empty_store()
    previous = (cache.get("generation") or {}).get(category) or {}
    entry = _generation_entry(fingerprint, success=False)
    if previous.get("generated_at") is not None:
        entry["generated_at"] = previous.get("generated_at")
    has_cached_text = _category_has_cached_text(cache, category)
    cache_action = "cached_text_preserved" if has_cached_text else "fallback_written"
    _emit_warning(
        "generation_failed",
        {
            "profile": task.profile if task else "",
            "category": category,
            "fingerprint": fingerprint[:12],
            "reason": reason,
            "provider": task.provider if task else None,
            "model": task.model if task else None,
            "retry_after": entry.get("retry_after"),
            "cache_action": cache_action,
        },
    )
    if not has_cached_text:
        fallback = _fallback_for_category(category, context or {})
        store.update_category(profile_path, category, fallback, entry)
        return
    store.update_generation_only(profile_path, category, entry)


def _system_prompt_for(category: str) -> str:
    if category == "emotion":
        return EMOTION_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def _load_user_prompt(category: str, context: dict[str, Any]) -> str:
    template = (PROMPTS_DIR / f"{category}.txt").read_text(encoding="utf-8")
    raw_skills_block = collectors.skills_block(context.get("skills") or [])
    skills_block = raw_skills_block if category == "skill" else collectors.sanitize_for_prompt(raw_skills_block, 1500)
    variables = {
        "display_name": collectors.sanitize_for_prompt(context.get("display_name"), 200),
        "description": collectors.sanitize_for_prompt(context.get("description"), 500),
        "soul": collectors.sanitize_for_prompt(context.get("soul"), 1500),
        "latest_memory": collectors.sanitize_for_prompt(context.get("latest_memory"), 500),
        "skills_count": str(context.get("skills_count") or 0),
        "skills_block": skills_block,
    }
    return template.format(**variables)


def _generate_with_model(
    task: BubbleTask,
    profile_path: Path,
    context: dict[str, Any],
) -> tuple[str | list[str] | None, str]:
    category = task.category
    system_prompt = _system_prompt_for(category)
    user_prompt = _load_user_prompt(category, context)
    prompt_meta = {
        "profile": task.profile,
        "category": category,
        "provider": task.provider,
        "model": task.model,
    }
    _emit_full_content_log(
        "model_prompt",
        prompt_meta,
        content_fields={
            "system_prompt": system_prompt,
            "user_prompt": _truncate_for_log(user_prompt, USER_PROMPT_LOG_MAX_CHARS),
        },
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    started = time.monotonic()
    try:
        auxiliary_client = importlib.import_module("agent.auxiliary_client")
        call_llm = getattr(auxiliary_client, "call_llm")
        from api.profiles import profile_env_for_background_worker

        with profile_env_for_background_worker(task.profile, purpose="assistant bubble generation"):
            resp = call_llm(
                task="assistant_bubbles",
                provider=task.provider,
                model=task.model,
                base_url=task.base_url,
                messages=messages,
                temperature=0.2,
                max_tokens=180 if category == "emotion" else 80,
                timeout=45,
            )
        content = resp.choices[0].message.content
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        _emit_warning(
            "model_call_failed",
            {
                "profile": task.profile,
                "category": category,
                "provider": task.provider,
                "model": task.model,
                "elapsed_ms": elapsed_ms,
                "error": exc,
            },
        )
        return None, "model_call_failed"
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    response_meta = {
        "profile": task.profile,
        "category": category,
        "provider": task.provider,
        "model": task.model,
        "elapsed_ms": elapsed_ms,
        **_content_metadata(content),
    }
    _emit_full_content_log(
        "model_response",
        response_meta,
        content_fields={"model_response": str(content or "")},
    )
    _emit_info("model_call_succeeded", response_meta)
    result, reason = validate_model_output(category, content, context)
    if result is None:
        _emit_warning(
            "model_output_rejected",
            {
                **response_meta,
                "reason": reason,
            },
        )
    return result, reason


def _strip_wrapping_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "“", "”", "‘", "’"}:
        return text[1:-1].strip()
    return text


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _skill_mentions_ascii_slug(value: str, context: dict[str, Any] | None) -> bool:
    skills = (context or {}).get("skills") or []
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if not name or _has_cjk(name):
            continue
        if name in value:
            return True
    return False


def _parse_emotion_json(raw: str) -> Any:
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


def validate_one_text(category: str, text: Any, context: dict[str, Any] | None = None) -> tuple[str | None, str]:
    if not isinstance(text, str):
        return None, "not_string"
    value = copy.normalize_emoji_punct(_strip_wrapping_quotes(text))
    if not value:
        return None, "empty"
    if "```" in value or re.search(r"(^|\n)\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+)", value):
        return None, "markdown_or_list_marker"
    if "\n" in value or "\r" in value:
        return None, "multiline"
    if category == "skill":
        if not _has_cjk(value):
            return None, "skill_not_chinese"
        if _skill_mentions_ascii_slug(value, context):
            return None, "skill_ascii_slug"
    return value, "ok"


def validate_model_output(
    category: str,
    content: Any,
    context: dict[str, Any] | None = None,
) -> tuple[str | list[str] | None, str]:
    if category == "emotion":
        if not isinstance(content, str):
            return None, "not_string"
        raw = content.strip()
        try:
            parsed = _parse_emotion_json(raw)
        except json.JSONDecodeError:
            return None, "invalid_json"
        if not isinstance(parsed, list):
            return None, "not_json_array"
        if len(parsed) != 4:
            return None, "emotion_count_not_4"
        out: list[str] = []
        seen: set[str] = set()
        for item in parsed:
            text, reason = validate_one_text(category, item, context)
            if not text:
                return None, f"emotion_item_{reason}"
            if text in seen:
                return None, "emotion_duplicate"
            out.append(text)
            seen.add(text)
        return out, "ok"
    return validate_one_text(category, content, context)
