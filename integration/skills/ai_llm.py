"""LLM call wrapper for SkillHub AI-meta generation.

Encapsulates prompt loading, message rendering, JSON extraction,
and call_llm invocation. All failures return None (graceful degradation).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_prompt_cache: dict[str, dict] = {}


def load_prompt(key: str) -> dict:
    """Load a YAML prompt template by key (filename without extension)."""
    if key in _prompt_cache:
        return _prompt_cache[key]
    path = _PROMPTS_DIR / f"{key}.yml"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {key}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _prompt_cache[key] = data
    return data


def render_messages(template: dict, variables: dict[str, str]) -> list[dict]:
    """Replace {{var}} placeholders in each message's content."""
    messages = template.get("messages", [])
    rendered = []
    for msg in messages:
        content = msg.get("content", "")
        for k, v in variables.items():
            content = content.replace("{{" + k + "}}", str(v or ""))
        rendered.append({"role": msg["role"], "content": content})
    return rendered


def _repair_truncated_json(text: str) -> str | None:
    """Attempt to repair JSON truncated by max_tokens cutoff.

    Closes unclosed strings, arrays, and objects so json.loads can succeed.
    Returns repaired string or None if repair fails.
    """
    s = text.rstrip()
    if not s:
        return None

    # If mid-string (odd number of unescaped quotes), close the string
    in_string = False
    escape = False
    quote_count = 0
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            quote_count += 1
            in_string = not in_string
    if in_string:
        s += '"'

    # Track open brackets/braces
    stack = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    # Remove trailing comma if any (before closing)
    s = re.sub(r',\s*$', '', s)

    # Close remaining open brackets/braces
    for opener in reversed(stack):
        if opener == '{':
            s += '}'
        elif opener == '[':
            s += ']'

    return s


def extract_json(text: str) -> dict | None:
    """Parse JSON from LLM response, stripping code fences if present."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # Attempt repair for truncated JSON
        repaired = _repair_truncated_json(text)
        if repaired and repaired != text:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                # Try regex match on repaired text
                m2 = re.search(r"\{.*\}", repaired, re.DOTALL)
                if m2:
                    try:
                        return json.loads(m2.group(0))
                    except json.JSONDecodeError:
                        pass
    return None


def _resolve_default_model() -> tuple[str | None, str | None]:
    """Read provider and model from hermes config (~/.hermes/config.yaml)."""
    try:
        import os
        config_path = Path(os.path.expanduser("~")) / ".hermes" / "config.yaml"
        if not config_path.is_file():
            return None, None
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str):
            return None, model_cfg.strip() or None
        if isinstance(model_cfg, dict):
            provider = str(model_cfg.get("provider", "")).strip() or None
            model = str(model_cfg.get("default", "")).strip() or None
            return provider, model
    except Exception:
        pass
    return None, None


def _do_llm_call(
    prompt_key: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    task: str,
    provider: str | None,
    model: str | None,
) -> str | None:
    """Execute a single LLM call, return response content or None."""
    try:
        from agent.auxiliary_client import call_llm

        resp = call_llm(
            task=task,
            provider=provider,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60,
        )
    except Exception as exc:
        _log.warning("ai_llm: LLM call failed for %s: %s", prompt_key, exc)
        return None

    try:
        return resp.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        _log.warning("ai_llm: empty response for %s: %s", prompt_key, exc)
        return None


def call_with_prompt(
    prompt_key: str,
    variables: dict[str, str],
    task: str = "skills_hub",
    retries: int = 1,
) -> dict | None:
    """Load prompt, render variables, call LLM, parse JSON response.

    Returns parsed JSON dict on success, None on any failure.
    Retries once with a stricter instruction if JSON parsing fails.
    """
    try:
        template = load_prompt(prompt_key)
    except Exception as exc:
        _log.warning("ai_llm: failed to load prompt %s: %s", prompt_key, exc)
        return None

    messages = render_messages(template, variables)
    temperature = template.get("temperature", 0.2)
    max_tokens = template.get("maxTokens", 2048)

    provider, model = _resolve_default_model()

    content = _do_llm_call(prompt_key, messages, temperature, max_tokens, task, provider, model)
    result = extract_json(content) if content else None

    # Retry with stricter instruction if JSON parsing failed
    if result is None and retries > 0 and content:
        _log.info("ai_llm: retrying %s with stricter JSON instruction", prompt_key)
        retry_messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "你的上一条回复不是合法JSON，请严格只输出一个完整的JSON对象，不要有任何其他文字。"},
        ]
        content2 = _do_llm_call(prompt_key, retry_messages, temperature, max_tokens, task, provider, model)
        result = extract_json(content2) if content2 else None

    if result is None:
        _log.warning("ai_llm: failed to parse JSON from %s response, raw content: %s", prompt_key, (content or "")[:500])
    return result
