"""Direct Google APIs: Calendar for events, Gmail for sending approved emails."""
import base64
from email.message import EmailMessage

from actions.base import ActionAdapter


class GoogleActions(ActionAdapter):
    backend = "google"

    def __init__(self, calendar=None, gmail=None):
        self._calendar, self._gmail = calendar, gmail

    @property
    def calendar(self):
        if self._calendar is None:
            import google_auth
            self._calendar = google_auth.service("calendar", "v3")
        return self._calendar

    @property
    def gmail(self):
        if self._gmail is None:
            import google_auth
            self._gmail = google_auth.service("gmail", "v1")
        return self._gmail

    def create_event(self, title, start, end, location_or_link, description):
        body = {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end},
                "description": description}
        if location_or_link:
            body["location"] = location_or_link
        return self.calendar.events().insert(calendarId="primary", body=body).execute()["id"]

    def update_event(self, event_id, **changes):
        body = {}
        if "title" in changes:
            body["summary"] = changes["title"]
        if "start" in changes:
            body["start"] = {"dateTime": changes["start"]}
        if "end" in changes:
            body["end"] = {"dateTime": changes["end"]}
        if "location_or_link" in changes:
            body["location"] = changes["location_or_link"] or ""
        if "description" in changes:
            body["description"] = changes["description"]
        self.calendar.events().patch(calendarId="primary", eventId=event_id, body=body).execute()

    def delete_event(self, event_id):
        self.calendar.events().delete(calendarId="primary", eventId=event_id).execute()

    def send_email(self, to, subject, body, reply_to_thread_id=None):
        msg = EmailMessage()
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if reply_to_thread_id:
            payload["threadId"] = reply_to_thread_id
        return self.gmail.users().messages().send(userId="me", body=payload).execute()["id"]

    def call_reminder(self, summary):
        return False  # no calls without Fo; the caller sends the summary on Telegram instead
