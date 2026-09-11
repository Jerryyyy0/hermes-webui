"""Pre-publish skill package format validation (upstream SkillHub contract).

``GET /api/skillhub/publish/validate`` and the submit-time gate share
``validate_skill_package``. The file set examined here mirrors what
``zip_pack.build_skill_zip`` would actually pack (dotfile segments skipped),
so the user sees exactly what upstream would reject.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

_MAX_PACKAGE_BYTES = 500 * 1024 * 1024  # upstream hard limit (500MB)
_NAME_CHARSET_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _issue(
    code: str,
    message: str,
    message_zh: str,
    *,
    severity: str = "error",
    **extra: Any,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "message_zh": message_zh,
    }
    issue.update(extra)
    return issue


def _packable_files(skill_dir: Path) -> Iterator[tuple[Path, Path]]:
    """Same file set build_skill_zip would pack (hidden marker files excluded)."""
    for fp in sorted(skill_dir.rglob("*")):
        rel = fp.relative_to(skill_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if fp.is_file():
            yield fp, rel


def validate_skill_package(skill_name: str, skill_dir: Path | str) -> list[dict[str, Any]]:
    """Return a list of issues; ``severity=error`` blocks submission."""
    src = Path(skill_dir)
    issues: list[dict[str, Any]] = []
    files = list(_packable_files(src))

    skill_mds = [(fp, rel) for fp, rel in files if fp.name.lower() == "skill.md"]
    canonical: Path | None = None
    if not skill_mds:
        issues.append(
            _issue(
                "SKILL_MD_REQUIRED",
                "SKILL.md is required",
                "缺少 SKILL.md：技能包内必须包含一个 SKILL.md（大小写敏感）",
            )
        )
    else:
        rels = [rel.as_posix() for _, rel in skill_mds]
        if len(skill_mds) > 1:
            issues.append(
                _issue(
                    "MULTIPLE_SKILL_MD",
                    "multiple SKILL.md found",
                    "检测到多个 SKILL.md（可能位于不同子目录或为大小写变体如 "
                    "skill.md/Skill.MD），请仅保留技能根目录下的一个",
                    files=rels,
                )
            )
        else:
            rel = skill_mds[0][1]
            canonical = skill_mds[0][0]
            if rel.parts != ("SKILL.md",):
                issues.append(
                    _issue(
                        "SKILL_MD_NOT_ROOT",
                        "SKILL.md must be at the skill package root",
                        "SKILL.md 须位于技能根目录（打包后为压缩包根），"
                        f"当前位于子目录 {rel.as_posix()}",
                        file=rel.as_posix(),
                    )
                )
    if canonical is not None:
        issues.extend(_check_skill_md_content(skill_name, canonical))

    junk = [
        rel.as_posix()
        for fp, rel in files
        if "__pycache__" in rel.parts or fp.name.endswith(".pyc")
    ]
    if junk:
        issues.append(
            _issue(
                "JUNK_FILE",
                "junk files will be packaged",
                "检测到 __pycache__/.pyc 等冗余文件，打包时会被一并上传，建议清理",
                severity="warning",
                files=junk,
            )
        )

    total = 0
    for fp, _ in files:
        try:
            total += fp.stat().st_size
        except OSError:
            pass
    if total > _MAX_PACKAGE_BYTES:
        issues.append(
            _issue(
                "PACKAGE_TOO_LARGE",
                "package exceeds the 500MB upstream limit",
                f"打包总大小超过上游 500MB 限制（当前约 {total // (1024 * 1024)}MB）",
                size_bytes=total,
            )
        )
    return issues


def _check_skill_md_content(skill_name: str, skill_md: Path) -> list[dict[str, Any]]:
    from integration.skills.validate import validate_skill_md_content

    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [
            _issue(
                "INVALID_SKILL_MD",
                "invalid SKILL.md content",
                "SKILL.md 无法读取或非 UTF-8 文本编码",
            )
        ]

    err = validate_skill_md_content(content)
    if err:
        return [
            _issue(
                "INVALID_SKILL_MD",
                "invalid SKILL.md content",
                str(err.get("error") or "SKILL.md 内容不合规"),
            )
        ]

    name = str(_frontmatter_name(content) or "")
    issues: list[dict[str, Any]] = []
    if name and not _NAME_CHARSET_RE.match(name):
        issues.append(
            _issue(
                "NAME_CHARSET",
                "name contains characters not allowed upstream "
                "(letters, digits, '-' and '_' only)",
                "frontmatter name 含上游不允许的字符（仅支持字母、数字、连字符和"
                "下划线；中文/空格会被上游替换为连字符导致命名异常）",
                name=name,
            )
        )
    if name and skill_name and name != str(skill_name):
        issues.append(
            _issue(
                "NAME_MISMATCH",
                "frontmatter name does not match skill_name",
                f"frontmatter name（{name}）与技能名（{skill_name}）不一致，"
                "可能导致上游定位与本地版本链追踪异常",
                severity="warning",
                name=name,
            )
        )
    return issues


def _frontmatter_name(content: str) -> str:
    try:
        from tools.skills_tool import _parse_frontmatter as parse
    except ImportError:
        from integration.skills.validate import _parse_frontmatter as parse
    try:
        fm, _ = parse(content)
    except Exception:
        return ""
    if isinstance(fm, dict):
        return str(fm.get("name") or "")
    return ""


def resolve_skill_dir(skill_name: str) -> Path | None:
    """Locate the local skill directory for pre-publish validation.

    Falls back to a directory-name scan when ``_find_skill_in_any_profile``
    cannot resolve (e.g. SKILL.md is missing or has a case variant), so the
    validator can report the actual problem instead of a bare 404.
    """
    raw = str(skill_name or "").strip()
    if not raw:
        return None
    try:
        from integration.skills.local_skills import _find_skill_in_any_profile

        skill_dir, _skill_md = _find_skill_in_any_profile(raw)
        if skill_dir is not None:
            return skill_dir
    except Exception:
        pass

    from integration.skills.local_skills import shared_skills_dir

    try:
        roots = [shared_skills_dir()]
    except Exception:
        return None
    wanted = raw.lower()
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for d in Path(root).rglob("*"):
            if d.is_dir() and d.name.lower() == wanted:
                return d
    return None
