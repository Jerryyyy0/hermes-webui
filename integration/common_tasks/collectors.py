"""Context collectors for common tasks mining and seed generation.

Reuses helpers from ``integration.assistant_bubbles.collectors`` (resolve_profile,
load_info, model_route, collect_skills, skills_block, sanitize_for_prompt,
_read_limited) rather than duplicating them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from integration.assistant_bubbles.collectors import (
    _read_limited,
    collect_skills,
    load_info,
    model_route,
    resolve_profile,
    sanitize_for_prompt,
    skills_block,
)

PROMPT_VERSION_SEED = "seed.v1"
PROMPT_VERSION_CLUSTER = "cluster.v1"


def collect_recent_user_questions(profile: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent user questions from WebUI sessions belonging to ``profile``.

    Reads ``SESSION_DIR/<sid>.json`` files whose ``profile`` field matches
    (defaulting to "default" when missing). Extracts plain text from each user
    message via ``api.session_ops._extract_text`` (handles string + multimodal
    list shapes). Skips empty text and slash commands.
    """
    from api.config import SESSION_DIR
    from api.session_ops import _extract_text

    target = str(profile or "default").strip()
    questions: list[dict[str, Any]] = []
    if not SESSION_DIR.exists():
        return questions
    for path in SESSION_DIR.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        row_profile = str(data.get("profile") or "default").strip() or "default"
        if row_profile != target:
            continue
        sid = str(data.get("session_id") or path.stem)
        messages = data.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = (_extract_text(msg.get("content")) or "").strip()
            if not text or text.startswith("/"):
                continue
            try:
                ts = float(msg.get("timestamp") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            questions.append({"text": text, "timestamp": ts, "session_id": sid})
    questions.sort(key=lambda x: x["timestamp"], reverse=True)
    return questions[:limit]


def fingerprint_for_cluster(questions: list[dict[str, Any]]) -> str:
    """SHA-256 over sorted unique question texts.

    Timestamps are deliberately excluded so that the same question set does
    not trigger re-mining just because time advanced.
    """
    texts = sorted({str(q.get("text") or "") for q in questions if q.get("text")})
    payload = {"v": PROMPT_VERSION_CLUSTER, "qs": texts}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def collect_seed_context(profile: str, profile_path: Path) -> dict[str, Any]:
    """Persona + skills context for seed generation (LLM first-time fallback)."""
    info = load_info(profile_path)
    skills = collect_skills(profile)
    return {
        "profile": profile,
        "display_name": str(info.get("display_name") or profile or "Hermes").strip() or "Hermes",
        "description": str(info.get("description") or "").strip(),
        "soul": _read_limited(profile_path / "SOUL.md"),
        "skills": skills,
        "skills_count": len(skills),
    }


def render_seed_user_prompt(context: dict[str, Any]) -> str:
    """Render the seed prompt template with persona + skills context."""
    prompt_path = Path(__file__).resolve().parent / "prompts" / "seed.txt"
    template = prompt_path.read_text(encoding="utf-8")
    raw_block = skills_block(context.get("skills") or [])
    return template.format(
        display_name=sanitize_for_prompt(context.get("display_name"), 200),
        description=sanitize_for_prompt(context.get("description"), 500),
        soul=sanitize_for_prompt(context.get("soul"), 1500),
        skills_block=sanitize_for_prompt(raw_block, 1500),
    )


def render_cluster_user_prompt(questions: list[dict[str, Any]]) -> str:
    """Render the cluster prompt template with numbered, de-duplicated questions."""
    prompt_path = Path(__file__).resolve().parent / "prompts" / "cluster.txt"
    template = prompt_path.read_text(encoding="utf-8")
    seen: set[str] = set()
    numbered: list[str] = []
    for q in questions:
        text = str(q.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        numbered.append(f"{len(numbered) + 1}. {text}")
    return template.format(
        n=len(numbered),
        numbered_questions="\n".join(numbered) or "(无)",
    )


__all__ = [
    "PROMPT_VERSION_SEED",
    "PROMPT_VERSION_CLUSTER",
    "collect_recent_user_questions",
    "fingerprint_for_cluster",
    "collect_seed_context",
    "render_seed_user_prompt",
    "render_cluster_user_prompt",
    "resolve_profile",
    "model_route",
]
