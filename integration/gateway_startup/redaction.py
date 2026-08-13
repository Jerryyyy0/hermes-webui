"""Strict redaction for Gateway child-process console output."""

from __future__ import annotations

import re

_AUTH_RE = re.compile(
    r"((?:proxy-)?authorization\s*:\s*(?:[a-z][\w.+-]*\s+)?)\S+",
    re.IGNORECASE,
)
_COOKIE_RE = re.compile(r"((?:set-)?cookie\s*:\s*)[^\r\n]+", re.IGNORECASE)
_SECRET_HEADER_RE = re.compile(
    r"((?:x-)?api[-_]?key\s*:\s*)\S+", re.IGNORECASE
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_.-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|COOKIE|AUTHORIZATION)[A-Za-z0-9_.-]*)\s*([=:])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_JSON_SECRET_RE = re.compile(
    r'("(?:token|secret|password|passwd|api[_-]?key|cookie|authorization)"\s*:\s*")[^"]*',
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:token|secret|password|passwd|api[_-]?key|access_token|refresh_token|authorization)=)[^&#\s]+",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_KNOWN_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")
_URL_USERINFO_RE = re.compile(r"(://)[^\s/@:]+:[^\s/@]+@")


def redact_gateway_console_line(line: str) -> str:
    """Return a console-safe line without relying on the Agent Python env."""
    text = str(line or "")
    text = _AUTH_RE.sub(r"\1<redacted>", text)
    text = _COOKIE_RE.sub(r"\1<redacted>", text)
    text = _SECRET_HEADER_RE.sub(r"\1<redacted>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = _JSON_SECRET_RE.sub(r"\1<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    text = _JWT_RE.sub("<redacted>", text)
    text = _KNOWN_TOKEN_RE.sub("<redacted>", text)
    return _URL_USERINFO_RE.sub(r"\1<redacted>@", text)
