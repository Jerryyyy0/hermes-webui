"""In-process Zhiling identity cache for single-user container deployments."""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass

from integration.config import zhiling_identity_cache_ttl_seconds

_LOCK = threading.Lock()
_store: _ZhilingSession | None = None


@dataclass
class _ZhilingSession:
    access_token: str
    identity: dict
    expires_at: float
    cached_at: float


def _jwt_exp_unix(token: str) -> float | None:
    """Decode JWT exp claim without signature verification (TTL hint only)."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".", 2)[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return float(exp)
    return None


def _resolve_expires_at(token: str, cached_at: float) -> float:
    jwt_exp = _jwt_exp_unix(token)
    if jwt_exp is not None:
        return jwt_exp
    return cached_at + float(zhiling_identity_cache_ttl_seconds())


def save_session(access_token: str, identity: dict) -> None:
    """Persist identity and token in memory after a successful Control Plane lookup."""
    global _store
    if not isinstance(access_token, str) or not access_token.strip():
        return
    if not isinstance(identity, dict):
        return
    now = time.time()
    session = _ZhilingSession(
        access_token=access_token.strip(),
        identity=identity,
        expires_at=_resolve_expires_at(access_token, now),
        cached_at=now,
    )
    with _LOCK:
        _store = session


def get_cached_identity() -> tuple[int, dict]:
    """Return cached identity without exposing the stored access token."""
    with _LOCK:
        session = _store
    if session is None:
        return 401, {"error": "not_registered"}
    if time.time() >= session.expires_at:
        return 401, {"error": "session_expired"}
    return 200, session.identity


def clear_session() -> None:
    """Drop the in-process Zhiling identity cache."""
    global _store
    with _LOCK:
        _store = None
