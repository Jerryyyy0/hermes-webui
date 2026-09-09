from types import SimpleNamespace

from api import process_event_utils as delivery
from api.models import Session


def test_ack_uses_origin_database_despite_foreign_home(tmp_path, monkeypatch):
    from tools import async_delegation as agent
    from hermes_constants import (
        get_hermes_home, set_hermes_home_override, reset_hermes_home_override,
    )

    origin = tmp_path / "origin"
    foreign = tmp_path / "foreign"
    monkeypatch.setattr(Session, "load", classmethod(
        lambda cls, sid: SimpleNamespace(profile="default")
    ))
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: origin)
    token = set_hermes_home_override(str(origin))
    try:
        with agent._connect() as db:
            db.execute(
                "INSERT INTO async_delegations "
                "(delegation_id, origin_session, state, dispatched_at, updated_at) "
                "VALUES ('profile-ack-test', 'origin-session', 'completed', 1, 1)"
            )
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(str(foreign))
    try:
        event = {"type": "async_delegation", "delegation_id": "profile-ack-test",
                 "origin_ui_session_id": "origin-session"}
        claim = delivery.claim_async_delegation_delivery(event, "test")
        assert claim is not None
        assert delivery.complete_async_delegation_delivery(event, claim) == "acknowledged"
        assert get_hermes_home() == foreign
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(str(origin))
    try:
        row = agent.get_durable_delegation("profile-ack-test")
        assert row["delivery_state"] == "delivered"
        assert row["delivery_attempts"] == 1
    finally:
        reset_hermes_home_override(token)
