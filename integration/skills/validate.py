"""Basic SKILL.md format validation for custom uploads."""

from __future__ import annotations

from integration.skills.utils import is_system_skill


def _max_description_length() -> int:
    try:
        from tools.skills_tool import MAX_DESCRIPTION_LENGTH

        return int(MAX_DESCRIPTION_LENGTH)
    except Exception:
        return 500


def _parse_frontmatter(content: str) -> tuple[dict | None, str]:
    text = str(content or "")
    if not text.lstrip().startswith("---"):
        return None, text
    try:
        from tools.skills_tool import _parse_frontmatter

        frontmatter, body = _parse_frontmatter(text)
        if not isinstance(frontmatter, dict):
            return None, body
        return frontmatter, body
    except ImportError:
        pass
    except Exception:
        return None, text

    parts = text.lstrip().split("---", 2)
    if len(parts) < 3:
        return None, text
    frontmatter: dict = {}
    for line in parts[1].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        frontmatter[key.strip()] = val.strip().strip('"').strip("'")
    return frontmatter, parts[2]


def _platform_error(frontmatter: dict) -> dict | None:
    try:
        from tools.skills_tool import skill_matches_platform
    except ImportError:
        return None
    try:
        if not skill_matches_platform(frontmatter):
            return {
                "error": "该技能不支持当前平台",
                "status": 400,
            }
    except Exception:
        return None
    return None


def _validate_name_field(name: str) -> dict | None:
    raw = str(name or "").strip()
    if not raw:
        return {"error": "SKILL.md frontmatter 缺少 name", "status": 400}
    if len(raw) > 64:
        return {"error": "name 最多 64 个字符", "status": 400}
    if "/" in raw or ".." in raw:
        return {"error": "frontmatter 中的 name 无效", "status": 400}
    if is_system_skill(raw):
        return {"error": "frontmatter 中的 name 无效", "status": 400}
    normalized = str(raw).strip().lower().replace(" ", "-")[:64]
    if not normalized:
        return {"error": "frontmatter 中的 name 无效", "status": 400}
    return None


def validate_skill_md_content(content: str) -> dict | None:
    """Return an error payload dict, or None when content is valid."""
    text = str(content or "")
    if not text.strip():
        return {"error": "SKILL.md 内容为空", "status": 400}

    frontmatter, _body = _parse_frontmatter(text)
    if frontmatter is None:
        return {
            "error": "SKILL.md 须以 YAML frontmatter（--- ... ---）开头",
            "status": 400,
        }

    name_err = _validate_name_field(str(frontmatter.get("name", "") or ""))
    if name_err:
        return name_err

    description = str(frontmatter.get("description", "") or "").strip()
    if not description:
        return {"error": "SKILL.md frontmatter 缺少 description", "status": 400}

    max_len = _max_description_length()
    if len(description) > max_len:
        return {
            "error": f"description 最多 {max_len} 个字符",
            "status": 400,
        }

    return _platform_error(frontmatter)
