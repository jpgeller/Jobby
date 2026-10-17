import pytest

import actions
import calendar_sync
import config
import db
import main
import pipeline
from actions.base import ActionAdapter
from actions.google_actions import GoogleActions


class FakeCalendar(ActionAdapter):
    backend = "google"

    def __init__(self, fail=False):
        self.events, self.calls, self.fail = {}, [], fail

    def create_event(self, title, start, end, location_or_link, description):
        if self.fail:
            raise RuntimeError("calendar down")
        event_id = f"cal{len(self.events) + 1}"
        self.events[event_id] = dict(title=title, start=start, end=end)
        self.calls.append(("create", event_id))
        return event_id

    def update_event(self, event_id, **changes):
        self.events[event_id].update({k: changes[k] for k in ("title", "start", "end")})
        self.calls.append(("update", event_id))


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


@pytest.fixture
def conn(tmp_path):
    return db.init_db(tmp_path / "test.db")


def process(conn, ids):
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for i in ids:
        pipeline.process_email(conn, emails[i])


def test_interview_creates_one_event_and_reschedule_updates_it(conn):
    cal = FakeCalendar()
    process(conn, ["fixture-01", "fixture-02", "fixture-03"])
    calendar_sync.sync_calendar(conn, cal)
    interview = [e for e in cal.events.values() if e["title"].startswith("Interview")]
    assert len(interview) == 1 and interview[0]["start"] == "2026-10-23T15:00:00-07:00"

    cal.calls.clear()
    process(conn, ["fixture-04"])  # reschedule
    calendar_sync.sync_calendar(conn, cal)
    assert [c[0] for c in cal.calls] == ["update"]
    interview = [e for e in cal.events.values() if e["title"].startswith("Interview")]
    assert len(interview) == 1 and interview[0]["start"] == "2026-10-26T11:00:00-07:00"


def test_replaying_everything_adds_nothing(conn):
    cal = FakeCalendar()
    process(conn, ["fixture-01", "fixture-02", "fixture-03", "fixture-04", "fixture-06"])
    calendar_sync.sync_calendar(conn, cal)
    count = len(cal.events)
    cal.calls.clear()
    process(conn, ["fixture-01", "fixture-02", "fixture-03", "fixture-04", "fixture-06"])
    calendar_sync.sync_calendar(conn, cal)
    assert cal.calls == [] and len(cal.events) == count == 3  # interview + OA deadline + offer deadline


def test_failure_is_logged_and_retried(conn):
    process(conn, ["fixture-03"])
    assert calendar_sync.sync_calendar(conn, FakeCalendar(fail=True)) == 0
    assert conn.execute("SELECT success FROM actions_log").fetchone()["success"] == 0
    assert calendar_sync.sync_calendar(conn, FakeCalendar()) == 1  # still dirty, so it retries


def test_disallowed_action_is_refused():
    with pytest.raises(actions.ActionError):
        actions.call(FakeCalendar(), "buy_something", item="laptop")


def test_google_send_email_builds_raw_message():
    sent = {}

    class Gmail:
        def users(self): return self
        def messages(self): return self
        def send(self, userId, body):
            sent.update(body)
            return self
        def execute(self): return {"id": "msg1"}

    assert GoogleActions(gmail=Gmail()).send_email("me@example.com", "Hi", "Body", "thread9") == "msg1"
    assert sent["threadId"] == "thread9" and sent["raw"]
