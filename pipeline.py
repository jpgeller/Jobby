"""Flow B (spec Section 6): one email in, database updated. The calendar, tracker, and Telegram
are synced from the database afterwards."""
from datetime import datetime, timedelta

import classifier
import db
import dedup


def source_from_sender(sender):
    sender = sender.lower()
    if "handshake" in sender:
        return "handshake"
    if "interstride" in sender:
        return "interstride"
    return "direct"


def process_email(conn, email, result=None):
    """Classify one email and write the result. Safe to call twice for the same email.
    Pass `result` (from classifier.classify_email) to skip classifying here, e.g. when the
    Claude call already ran in a worker thread.

    Returns a dict with `action`: already_processed | ignored | needs_review | recruiter_lead |
    created | updated | unchanged, plus application_id, old_status, new_status, event_id."""
    if db.is_email_processed(conn, email["id"]):
        return {"action": "already_processed"}

    result = result or classifier.classify_email(email)
    outcome = _apply(conn, email, result)
    label = "needs_review" if outcome["action"] == "needs_review" else result["category"]
    db.mark_email_processed(conn, email["id"], label, outcome.get("application_id"),
                            from_address=email.get("from"), thread_id=email.get("thread_id"))
    conn.commit()
    outcome["classification"] = result
    return outcome


def _apply(conn, email, result):
    category = result["category"]
    if category == "not_relevant":
        return {"action": "ignored"}
    if result["needs_review"]:
        return {"action": "needs_review"}

    proposed = dedup.CATEGORY_TO_STATUS.get(category)
    app = dedup.find_application(conn, result["company"], result["role"])

    if app is None:
        if proposed is None:
            # A recruiter wrote first about a role we never applied to: not an application yet.
            return {"action": "recruiter_lead"}
        app_id = db.insert_application(
            conn,
            company=result["company"],
            company_norm=dedup.normalize_company(result["company"]),
            role=result["role"],
            source=source_from_sender(email["from"]),
            status=proposed,
            applied_date=email["received"][:10],
            next_step=result["next_step"],
            next_deadline=result["deadline"],
        )
        outcome = {"action": "created", "application_id": app_id, "old_status": None, "new_status": proposed}
    else:
        app_id, current = app["id"], app["status"]
        new = dedup.next_status(current, proposed)
        fields = {}
        if new != current:
            fields["status"] = new
        # Only an email that is current (not an older stage arriving late) may change the next step.
        if proposed is None or new == proposed:
            if result["next_step"] or proposed:
                fields["next_step"] = result["next_step"]
            if proposed:
                fields["next_deadline"] = result["deadline"]
        changed = {k: v for k, v in fields.items() if app[k] != v}
        db.update_application(conn, app_id, **changed)
        if "status" in changed:
            # The application moved to a new stage, so deadlines from the old one (e.g. the OA
            # once an interview is booked) no longer need reminders. Calendar events stay.
            conn.execute("UPDATE events SET reminded_24h = 1, reminded_2h = 1 "
                         "WHERE application_id = ? AND type = 'deadline'", (app_id,))
        outcome = {"action": "updated" if changed else "unchanged", "application_id": app_id,
                   "old_status": current, "new_status": new}

    if outcome["new_status"] == proposed:
        outcome["event_id"], event_changed = _record_event(conn, app_id, result["event"], result, category,
                                                           email["received"])
        _, deadline_changed = _record_event(conn, app_id, _deadline_event(result), result, category,
                                            email["received"])
        if (event_changed or deadline_changed) and outcome["action"] == "unchanged":
            outcome["action"] = "updated"
    return outcome


def _deadline_event(result):
    """A deadline (OA due date, offer reply) becomes a 30-minute calendar block ending at it."""
    if not result["deadline"]:
        return None
    due = datetime.fromisoformat(result["deadline"])
    return {"type": "deadline", "start": (due - timedelta(minutes=30)).isoformat(), "end": due.isoformat(),
            "location_or_link": None}


def _record_event(conn, app_id, event, result, category, received):
    """Insert or move the event this email describes. Returns (event_id, changed)."""
    if not event or not event["start"]:
        return None, False
    fields = {
        "start": event["start"],
        "end": event["end"],
        "location_or_link": event["location_or_link"],
        "email_received_at": received,
    }
    existing = db.latest_event(conn, app_id, event["type"])
    # Same time, or an email no newer than the one that last set the event (a copy, or an
    # old invite arriving after a reschedule): nothing to change.
    if existing and (existing["start"] == event["start"] or received <= (existing["email_received_at"] or "")):
        return existing["id"], False
    if category == "interview_reschedule" and existing:
        # New time means reminders and the prep call must fire again.
        db.update_event(conn, existing["id"], **fields, reminded_24h=0, reminded_2h=0, called=0, calendar_dirty=1)
        return existing["id"], True
    if event["type"] == "deadline":
        title = f"Deadline: {result['next_step'] or 'reply'} - {result['company']}"
    else:
        title = f"{event['type'].capitalize()}: {result['company']} - {result['role']}"
    return db.insert_event(conn, application_id=app_id, type=event["type"], title=title, **fields), True
