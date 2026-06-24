import base64
import json
import time
from unittest.mock import patch

from integration.identity.session_store import (
    clear_session,
    get_cached_identity,
    save_session,
)


def _jwt_token(exp: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode("utf-8")
    ).decode("utf-8").rstrip("=")
    return f"header.{payload}.sig"


def setup_function():
    clear_session()


def test_save_and_get_roundtrip():
    identity = {"username": "zhangsan", "ithinktank": {"account": "zhangsan"}}
    save_session("plain-token", identity)
    status, payload = get_cached_identity()
    assert status == 200
    assert payload == identity


def test_clear_session_returns_not_registered():
    save_session("tok", {"username": "a"})
    clear_session()
    status, payload = get_cached_identity()
    assert status == 401
    assert payload == {"error": "not_registered"}


def test_jwt_exp_controls_cache_expiry():
    future = int(time.time()) + 3600
    save_session(_jwt_token(future), {"username": "jwt-user"})
    status, payload = get_cached_identity()
    assert status == 200
    assert payload["username"] == "jwt-user"

    past = int(time.time()) - 10
    save_session(_jwt_token(past), {"username": "expired"})
    status, payload = get_cached_identity()
    assert status == 401
    assert payload == {"error": "session_expired"}


def test_non_jwt_uses_configured_ttl():
    identity = {"username": "ttl-user"}
    now = 1_700_000_000.0
    with patch("integration.identity.session_store.time.time", return_value=now):
        with patch(
            "integration.identity.session_store.zhiling_identity_cache_ttl_seconds",
            return_value=60,
        ):
            save_session("plain-token", identity)

    with patch("integration.identity.session_store.time.time", return_value=now + 30):
        status, payload = get_cached_identity()
        assert status == 200
        assert payload == identity

    with patch("integration.identity.session_store.time.time", return_value=now + 61):
        status, payload = get_cached_identity()
        assert status == 401
        assert payload == {"error": "session_expired"}


def test_save_rejects_empty_token_or_non_dict_identity():
    save_session("", {"username": "x"})
    save_session("tok", [])
    status, payload = get_cached_identity()
    assert status == 401
    assert payload == {"error": "not_registered"}
