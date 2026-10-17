"""FoActions with a fake Fo and a fake Google: Fo's word is checked, and failures fall back."""
import pytest

import actions
from actions.base import ActionAdapter
from actions.fo_actions import FoActions

START, END = "2026-10-23T15:00:00-07:00", "2026-10-23T15:45:00-07:00"
EVENT = dict(title="Interview: Northwind", start=START, end=END, location_or_link="https://zoom.example/j/1",
             description="d")
EMAIL = dict(to="me@uni.edu", subject="[Jobby outreach test] Hi", body="Body")


class Req:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeCalendarAPI:
    def __init__(self):
        self.store = {}

    def events(self):
        return self

    def get(self, calendarId, eventId):
        return Req(self.store.get(eventId) or KeyError(eventId))

    def list(self, **kw):
        return Req({"items": [dict(e, id=i) for i, e in self.store.items()]})


class FakeGmailAPI:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return Req({"messages": [{"id": m} for m in self.sent]})


class FakeGoogle(ActionAdapter):
    """The read-only checker FoActions uses (calendar/gmail APIs) and the fallback backend."""
    backend = "google"

    def __init__(self):
        self.calendar, self.gmail = FakeCalendarAPI(), FakeGmailAPI()
        self.events, self.sent = self.calendar.store, self.gmail.sent
        self.created_by_google, self.sent_by_google = [], []

    def create_event(self, title, start, end, location_or_link, description):
        self.created_by_google.append(title)
        self.events["g1"] = {"summary": title, "start": {"dateTime": start}}
        return "g1"

    def send_email(self, to, subject, body, reply_to_thread_id=None):
        self.sent_by_google.append(subject)
        return "gmsg"

    def call_reminder(self, summary):
        return False


@pytest.fixture
def google():
    return FakeGoogle()


def fo(google, reply_text, status="complete", effect=None):
    def ask(message):
        if effect:
            effect(message)
        return {"status": status, "response": reply_text}
    return FoActions(google, ask=ask)


def test_event_created_by_fo_and_verified(google):
    adapter = fo(google, "EVENT_ID: fo123",
                 effect=lambda m: google.events.update({"fo123": {"summary": EVENT["title"], "start": {"dateTime": START}}}))
    result, entries = actions.call(adapter, "create_event", **EVENT)
    assert result == "fo123" and [e[0] for e in entries] == ["fo"] and google.created_by_google == []


def test_unclear_reply_but_event_exists_is_not_duplicated(google):
    adapter = fo(google, "Done! It's on your calendar.",
                 effect=lambda m: google.events.update({"fo999": {"summary": EVENT["title"], "start": {"dateTime": START}}}))
    result, _ = actions.call(adapter, "create_event", **EVENT)
    assert result == "fo999" and google.created_by_google == []


def test_fo_question_falls_back_to_google_and_both_are_logged(google):
    adapter = fo(google, "You already have something at 3pm. Should I still create it?")
    result, entries = actions.call(adapter, "create_event", **EVENT)
    assert result == "g1" and google.created_by_google == [EVENT["title"]]
    assert [(e[0], e[-1]) for e in entries] == [("fo", False), ("google", True)]


def test_failed_or_still_working_falls_back(google):
    for adapter in [fo(google, "FAILED: no calendar access"), fo(google, "", status="failed")]:
        google.created_by_google.clear()
        actions.call(adapter, "create_event", **EVENT)
        assert google.created_by_google == [EVENT["title"]]


def test_draft_instead_of_send_falls_back_to_google(google):
    adapter = fo(google, "I saved it as a draft in your Gmail. SENT_ID: none")
    result, entries = actions.call(adapter, "send_email", **EMAIL)
    assert result == "gmsg" and google.sent_by_google == [EMAIL["subject"]]
    assert [(e[0], e[-1]) for e in entries] == [("fo", False), ("google", True)]


def test_email_found_in_sent_is_not_sent_twice(google):
    adapter = fo(google, "Sent!", effect=lambda m: google.sent.append("fomsg"))
    result, entries = actions.call(adapter, "send_email", **EMAIL)
    assert result == "fomsg" and google.sent_by_google == [] and [e[0] for e in entries] == ["fo"]


def test_requests_are_fully_specified_and_scoped(google):
    seen = []
    adapter = fo(google, "FAILED: test", effect=seen.append)
    actions.call(adapter, "send_email", **EMAIL)
    msg = seen[0]
    assert msg.startswith("Automated request from Jobby") and "Do exactly this one action" in msg
    assert "To: me@uni.edu" in msg and "Subject: [Jobby outreach test] Hi" in msg and "Do not save it as a draft" in msg


def test_allow_list_still_applies_to_fo(google):
    with pytest.raises(actions.ActionError):
        actions.call(fo(google, "ok"), "make_purchase", item="laptop")


def test_call_needs_explicit_confirmation(google):
    assert actions.call(fo(google, "CALL_PLACED"), "call_reminder", summary="Hi")[0] is True
    seen = []
    result, entries = actions.call(fo(google, "I'll call you later today.", effect=seen.append), "call_reminder", summary="Hi")
    assert result is False  # Google's call_reminder: no call, so the bot sends the summary on Telegram
    assert [(e[0], e[-1]) for e in entries] == [("fo", False), ("google", True)]
    assert "only that number - never anyone else" in seen[0]


def test_email_request_states_the_approval(google):
    seen = []
    actions.call(fo(google, "FAILED: test", effect=seen.append), "send_email", **EMAIL)
    assert "tapped Approve" in seen[0]
