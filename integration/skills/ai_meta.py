"""AI-meta generation: 3-step LLM orchestration for skill metadata enrichment.

Step 1: Extract English name + description (if missing).
Step 2 (parallel): Translate to Chinese.
Step 3 (parallel): Extract structured detail JSON.

Any step failure yields null/empty — never raises.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from integration.skills.ai_llm import call_with_prompt

_log = logging.getLogger(__name__)


def generate_ai_meta(
    skill_md_content: str,
    name: str = "",
    description: str = "",
) -> dict:
    """Generate enriched skill metadata via 3 LLM calls.

    Returns::

        {
            "name": str | None,
            "description": str | None,
            "skillName": str | None,
            "displayDescription": str | None,
            "detailJson": dict | None,
        }
    """
    result = {
        "name": name or None,
        "description": description or None,
        "skillName": None,
        "displayDescription": None,
        "detailJson": None,
    }

    # ── Step 1: Extract name + description if missing ──
    if not name or not description:
        extracted = call_with_prompt(
            "skill-meta-extract",
            {"skillMdContent": skill_md_content},
        )
        if extracted:
            if not name:
                result["name"] = extracted.get("name") or None
            if not description:
                result["description"] = extracted.get("description") or None

    final_name = result["name"] or ""
    final_desc = result["description"] or ""

    # ── Step 2 & 3: parallel ──
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_translate = pool.submit(
            call_with_prompt,
            "skill-meta-translate",
            {"name": final_name, "description": final_desc},
        )
        future_detail = pool.submit(
            call_with_prompt,
            "skill-detail-extract",
            {"skillMdContent": skill_md_content},
        )

        translated = future_translate.result()
        detail = future_detail.result()

    if translated:
        result["skillName"] = translated.get("skill_name") or None
        result["displayDescription"] = translated.get("display_description") or None

    if detail:
        result["detailJson"] = detail
        _log.info("generate_ai_meta detailJson extracted: %s", detail)
    else:
        _log.warning("generate_ai_meta detailJson is empty — skill-detail-extract returned None")

    return result
