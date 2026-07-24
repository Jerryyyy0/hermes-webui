"""Language guidance for WebUI-originated clarify prompts."""


_CLARIFY_LANGUAGE_RULE = (
    "When calling the clarify tool in this WebUI session, write its question and "
    "every choice in Simplified Chinese. Keep code identifiers, commands, paths, "
    "URLs, and exact configuration values unchanged when they are needed for the "
    "user to make an informed decision.\n"
)


def clarify_language_rule() -> str:
    """Return the fixed WebUI instruction for clarify tool arguments."""
    return _CLARIFY_LANGUAGE_RULE
