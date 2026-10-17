"""Pick the action backend, enforce the allow-list, and log every action (spec Section 9)."""
import json
import logging

import config
import db

log = logging.getLogger(__name__)

# Jobby may only do these. Anything else is refused before it reaches a backend.
ALLOWED = {"create_event", "update_event", "delete_event", "send_email", "call_reminder"}


class ActionError(Exception):
    pass


def get_actions(backend=None):
    """FoActions with GoogleActions as its fallback, or GoogleActions alone."""
    import fo_client
    from actions.fo_actions import FoActions
    from actions.google_actions import GoogleActions

    google = GoogleActions()
    if (backend or config.ACTIONS_BACKEND) != "fo":
        return google
    if not fo_client.is_logged_in():
        log.warning("ACTIONS_BACKEND=fo but Jobby isn't logged in to Fo (python fo_client.py login); using Google")
        return google
    return FoActions(google)


def call(adapter, action, **kwargs):
    """Run one allow-listed action, falling back to adapter.google if the primary backend fails.
    Never touches the database, so it can run in a worker thread.
    Returns (result, log_entries); pass log_entries to record() on the main thread. The action
    succeeded if the last entry's success flag is true."""
    if action not in ALLOWED:
        raise ActionError(f"Action {action!r} is not allowed")
    request = json.dumps(kwargs, default=str)
    entries = []
    for backend in [adapter, getattr(adapter, "google", None)]:
        if backend is None:
            break
        try:
            result = getattr(backend, action)(**kwargs)
            entries.append((backend.backend, action, request, json.dumps(result, default=str), True))
            return result, entries
        except Exception as e:
            log.warning("Action %s via %s failed: %s", action, backend.backend, e)
            entries.append((backend.backend, action, request, repr(e), False))
    return None, entries


def succeeded(entries):
    return bool(entries) and entries[-1][-1]


def record(conn, entries):
    for entry in entries:
        db.log_action(conn, *entry)
