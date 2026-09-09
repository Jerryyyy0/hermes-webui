"""Bind Agent delivery database access to the originating session."""

from contextlib import contextmanager
from functools import wraps


@contextmanager
def session_delivery_scope(session):
    from api.profiles import get_hermes_home_for_profile
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = get_hermes_home_for_profile(getattr(session, "profile", None) or "default")
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def origin_delivery_scope(function):
    @wraps(function)
    def scoped(evt, *args, **kwargs):
        from api.models import Session

        sid = (evt.get("origin_ui_session_id") or evt.get("parent_session_id")
               or evt.get("session_key")) if isinstance(evt, dict) else None
        session = Session.load(str(sid)) if sid else None
        if session is None:
            return function(evt, *args, **kwargs)
        with session_delivery_scope(session):
            return function(evt, *args, **kwargs)
    return scoped
