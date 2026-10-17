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


def get_actions():
    from actions.google_actions import GoogleActions

    if config.ACTIONS_BACKEND == "fo":
        log.warning("ACTIONS_BACKEND=fo but FoActions arrives in milestone 11; using Google directly")
    return GoogleActions()


def call(adapter, action, **kwargs):
    """Run one allow-listed action. Never touches the database, so it can run in a worker thread.
    Returns (result, log_entry); pass log_entry to record() on the main thread."""
    if action not in ALLOWED:
        raise ActionError(f"Action {action!r} is not allowed")
    request = json.dumps(kwargs, default=str)
    try:
        result = getattr(adapter, action)(**kwargs)
        return result, (adapter.backend, action, request, json.dumps(result, default=str), True)
    except Exception as e:
        log.exception("Action %s failed", action)
        return None, (adapter.backend, action, request, repr(e), False)


def record(conn, log_entry):
    db.log_action(conn, *log_entry)
