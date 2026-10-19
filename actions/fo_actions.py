"""Wajo's Fo as the action layer (spec Section 9).

Fo takes natural-language requests, so each one is fully specified (exact times with offsets,
exact recipient and body) and asks for a fixed reply format. Fo's word is then checked against
Google directly, read-only: an unclear reply must never cause a duplicate event or a second email.
Any failure raises FoActionError, and actions.call() falls back to GoogleActions."""
import re
import time
from datetime import datetime, timedelta

import fo_client
from actions.base import ActionAdapter

PREAMBLE = ("Automated request from Jobby, my internship tracker. Do exactly this one action and nothing else. "
            "Do not ask follow-up questions and do not check for conflicts; if anything stops you, do nothing "
            "and reply with one line: FAILED: <reason>.\n\n")


# Calls get their own preamble: the calendar one's "don't check for conflicts" reads, on a phone call,
# like asking Fo to skip its safety checks, and Fo rightly refuses that.
CALL_PREAMBLE = "Request from Jobby, my internship tracker, on my behalf. Do only this one action.\n\n"


class FoActionError(Exception):
    pass


class FoActions(ActionAdapter):
    backend = "fo"

    def __init__(self, google, ask=None):
        """google: GoogleActions, used read-only to verify Fo's work (and by actions.call as the fallback)."""
        self.google = google
        self.ask = ask or fo_client.ask_fo

    # ----- calendar -----

    def create_event(self, title, start, end, location_or_link, description):
        reply = self._ask(
            "Create one Google Calendar event on my primary calendar:\n"
            f"- Title: {title}\n- Start: {start}\n- End: {end}\n"
            f"- Location: {location_or_link or '(none)'}\n- Description: {description}\n"
            "No invitees, no notifications.\n\nReply with one line: EVENT_ID: <the Google Calendar event id>")
        event_id = _field(reply, "EVENT_ID")
        if event_id and self._event_matches(event_id, start):
            return event_id
        # The reply was unclear; if Fo created it anyway, use that event rather than making a second one.
        found = self._find_event(title, start)
        if found:
            return found
        raise FoActionError(f"Fo did not confirm the event: {reply[:300]}")

    def update_event(self, event_id, **changes):
        lines = [f"- {k.replace('_', ' ').capitalize()}: {v}" for k, v in changes.items() if k != "description"]
        reply = self._ask(f"Update the Google Calendar event with id {event_id} on my primary calendar. "
                          f"Change only these fields:\n" + "\n".join(lines) +
                          "\nNo notifications.\n\nReply with one line: UPDATED: <event id>")
        if "start" in changes and not self._event_matches(event_id, changes["start"]):
            raise FoActionError(f"Fo did not update the event: {reply[:300]}")
        if "start" not in changes and _field(reply, "UPDATED") != event_id:
            raise FoActionError(f"Fo did not confirm the update: {reply[:300]}")

    def delete_event(self, event_id):
        # Only used by --demo-reset on Jobby's own events; deterministic and fast through Google.
        self.google.delete_event(event_id)

    # ----- email -----

    def send_email(self, to, subject, body, reply_to_thread_id=None):
        thread = (f"Send it as a reply in my Gmail thread with id {reply_to_thread_id}.\n"
                  if reply_to_thread_id else "")
        started = int(time.time()) - 5
        # Jobby only reaches this after the owner tapped Approve on this exact text in Telegram
        # (outreach.approve + send_request); Fo is told so, plainly, because it rightly asks for consent.
        reply = self._ask(
            "I reviewed this exact email in Jobby and tapped Approve to send it; this request carries that "
            "approval. Send it now from my connected email account, exactly as written. Do not save it as a draft, "
            "do not add a signature, do not change any text.\n"
            f"{thread}To: {to}\nSubject: {subject}\nBody:\n{body}\n\n"
            "Reply with one line: SENT_ID: <the sent message id>")
        # A sent email is confirmed by finding it in Sent, not by Fo's words ("saved as a draft" happens).
        found = self._find_sent(to, subject, started)
        if found:
            return found
        raise FoActionError(f"Fo did not send the email: {reply[:300]}")

    def call_reminder(self, summary, context=None):
        """Fo phones the account owner (the number on their Wajo profile) and reads the summary.
        True only if Fo confirms the call was placed. `context` says truthfully why the call is
        happening now; Fo uses its own judgment (e.g. about the hour), which Jobby doesn't override."""
        reply = self._ask(
            "Call me now on my own phone number from my profile, and only that number - never anyone else. "
            f"{context or ''}\nRead me this reminder, then hang up:\n\n"
            f"\"{summary}\"\n\n"
            "Reply with one line: CALL_PLACED, or FAILED: <reason>", preamble=CALL_PREAMBLE)
        if "CALL_PLACED" not in reply.upper():
            raise FoActionError(f"Fo did not confirm the call: {reply[:300]}")
        return True

    # ----- helpers -----

    def _ask(self, request, preamble=PREAMBLE):
        reply = self.ask(preamble + request)
        if reply.get("status") != "complete":
            raise FoActionError(f"Fo {reply.get('status')}: {reply.get('response') or reply.get('progress')}")
        text = reply.get("response") or ""
        if text.strip().upper().startswith("FAILED"):
            raise FoActionError(text[:300])
        return text

    def _event_matches(self, event_id, start):
        try:
            event = self.google.calendar.events().get(calendarId="primary", eventId=event_id).execute()
        except Exception:
            return False
        if event.get("status") == "cancelled":
            return False
        return _same_time(event["start"].get("dateTime"), start)

    def _find_event(self, title, start):
        t = datetime.fromisoformat(start)
        items = self.google.calendar.events().list(
            calendarId="primary", timeMin=(t - timedelta(minutes=1)).isoformat(),
            timeMax=(t + timedelta(minutes=1)).isoformat(), singleEvents=True).execute().get("items", [])
        for event in items:
            if event.get("summary") == title and _same_time(event["start"].get("dateTime"), start):
                return event["id"]
        return None

    def _find_sent(self, to, subject, after):
        """Only messages sent after this request started, so an earlier identical test can't match."""
        query = f'in:sent to:{to} subject:"{subject}" after:{after}'
        msgs = self.google.gmail.users().messages().list(userId="me", q=query, maxResults=1).execute()
        found = msgs.get("messages") or []
        return found[0]["id"] if found else None


def _field(text, name):
    match = re.search(rf"{name}:\s*([A-Za-z0-9_\-]+)", text or "")
    return match.group(1) if match else None


def _same_time(a, b):
    return bool(a and b) and datetime.fromisoformat(a) == datetime.fromisoformat(b)
