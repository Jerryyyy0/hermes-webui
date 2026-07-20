"""Fork-specific session-title language policy."""


_FIXED_CHINESE_TITLE_RULE = "Write the title in Simplified Chinese.\n"


def title_language_rule(_: str) -> str:
    """Return the fixed language instruction for every WebUI title request."""
    return _FIXED_CHINESE_TITLE_RULE


def should_validate_source_language_match() -> bool:
    """WebUI titles are intentionally independent of the source message language."""
    return False