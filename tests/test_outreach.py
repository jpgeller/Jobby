from datetime import date

import pytest

import actions
import config
import db
import main
import outreach
import pipeline
import research
from actions.base import ActionAdapter

TODAY = date(2026, 10, 17)


class FakeGmail(ActionAdapter):
    backend = "google"

    def __init__(self):
        self.sent = []

    def send_email(self, to, subject, body, reply_to_thread_id=None):
        self.sent.append(dict(to=to, subject=subject, body=body, thread=reply_to_thread_id))
        return f"msg{len(self.sent)}"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_CHAT_ID", "42")
    monkeypatch.setattr(config, "OUTREACH_SEND_TO_SELF", True)
    monkeypatch.setattr(config, "OUTREACH_MAX_PER_WEEK", 3)


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for i in ["fixture-01", "fixture-02"]:  # Northwind: confirmation (Handshake) + OA (recruiting@)
        pipeline.process_email(conn, dict(emails[i], thread_id=f"thread-{i}"))
    for company, applied in [("Greenfield Energy", "2026-09-28"), ("Pixelworks", "2026-10-14")]:
        db.insert_application(conn, company=company, company_norm=company.lower(), role="Intern",
                              status="applied", applied_date=applied)
    conn.commit()
    return conn


def app_named(conn, company):
    return next(a for a in db.list_applications(conn) if a["company"] == company)


def suggest(conn, company, **kw):
    app = app_named(conn, company)
    data = research.fetch(app["company"], app["role"])
    contact = outreach.pick_contact(conn, app, data, today=TODAY, **kw)
    email = outreach.draft(outreach.build_prompt(app, data, contact))
    return outreach.save_suggestion(conn, app["id"], contact, email), contact


def test_recruiter_who_emailed_is_the_contact_and_handshake_is_not(conn):
    contact = outreach.pick_contact(conn, app_named(conn, "Northwind Robotics"), None, today=TODAY)
    assert contact["to"] == "recruiting@northwind-robotics.example"
    assert contact["thread_id"] == "thread-fixture-02"  # reply in the recruiter's thread


def test_published_contact_needs_7_days_without_reply(conn):
    data = research.fetch("Pixelworks")
    assert data["contact_email"] == "recruiting@pixelworks.example" and data["contact_url"]
    assert outreach.pick_contact(conn, app_named(conn, "Pixelworks"), data, today=TODAY) is None  # 3 days
    assert outreach.pick_contact(conn, app_named(conn, "Pixelworks"), data, today=TODAY, manual=True)
    green = outreach.pick_contact(conn, app_named(conn, "Greenfield Energy"), research.fetch("Greenfield Energy"), today=TODAY)
    assert green["reason"] == "You applied 19 days ago and haven't heard back."


def test_no_contact_is_ever_guessed(conn):
    no_contact = {"summary": "x", "recent_news": "", "sources": [], "contact_email": None, "contact_url": None}
    assert outreach.pick_contact(conn, app_named(conn, "Greenfield Energy"), no_contact, today=TODAY, manual=True) is None


def test_nothing_sends_without_approval(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    with pytest.raises(PermissionError):
        outreach.send_request(conn, oid, "me@uni.edu")


def test_only_the_owner_can_approve_and_only_once(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    assert outreach.approve(conn, oid, 999) == (False, "Only the owner's chat can approve outreach.")
    assert outreach.approve(conn, oid, 42) == (True, "Approved.")
    assert outreach.approve(conn, oid, 42)[0] is False  # double tap


def test_approve_sends_to_self_in_test_mode_and_logs_it(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    outreach.approve(conn, oid, 42)
    gmail = FakeGmail()
    sent_id, entry = actions.call(gmail, "send_email", **outreach.send_request(conn, oid, "me@uni.edu"))
    actions.record(conn, entry)
    outreach.mark_sent(conn, oid, sent_id, today=TODAY)
    assert gmail.sent[0]["to"] == "me@uni.edu"
    assert gmail.sent[0]["subject"].startswith("[Jobby outreach test] ")
    assert "In real mode this goes to recruiting@greenfield-energy.example" in gmail.sent[0]["body"]
    assert outreach.get(conn, oid)["status"] == "sent"
    assert "Outreach sent to recruiting@greenfield-energy.example on 2026-10-17 (test, to self)" in \
        app_named(conn, "Greenfield Energy")["notes"]
    assert conn.execute("SELECT action, success FROM actions_log").fetchone()[:] == ("send_email", 1)


def test_real_mode_sends_to_recruiter_in_thread(conn, monkeypatch):
    monkeypatch.setattr(config, "OUTREACH_SEND_TO_SELF", False)
    oid, _ = suggest(conn, "Northwind Robotics", manual=True)
    outreach.approve(conn, oid, 42)
    request = outreach.send_request(conn, oid)
    assert request["to"] == "recruiting@northwind-robotics.example"
    assert request["reply_to_thread_id"] == "thread-fixture-02"


def test_skip_sends_nothing_and_is_never_suggested_again(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    outreach.skip(conn, oid)
    with pytest.raises(PermissionError):
        outreach.send_request(conn, oid, "me@uni.edu")
    assert outreach.approve(conn, oid, 42)[0] is False
    assert "Greenfield Energy" not in [a["company"] for a in outreach.candidates(conn)]


def test_one_per_application_and_weekly_cap(conn, monkeypatch):
    oid, _ = suggest(conn, "Greenfield Energy")
    outreach.approve(conn, oid, 42)
    outreach.mark_sent(conn, oid, "m1")
    second, _ = suggest(conn, "Greenfield Energy", manual=True)
    assert outreach.approve(conn, second, 42)[1].startswith("You already sent")
    monkeypatch.setattr(config, "OUTREACH_MAX_PER_WEEK", 1)
    other, _ = suggest(conn, "Pixelworks", manual=True)
    assert outreach.approve(conn, other, 42)[1].startswith("Weekly limit reached")


def test_failed_send_can_be_retried(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    outreach.approve(conn, oid, 42)
    outreach.mark_failed(conn, oid)
    assert outreach.approve(conn, oid, 42)[0] is True


def test_draft_is_short_and_names_the_role(conn):
    oid, _ = suggest(conn, "Greenfield Energy")
    o = outreach.get(conn, oid)
    assert outreach.word_count(o["body"]) < outreach.MAX_WORDS
    assert "Intern role at Greenfield Energy" in o["body"]
    revised = outreach.draft(outreach.build_prompt(app_named(conn, "Greenfield Energy"), research.fetch("Greenfield Energy"),
                                                   {"to": o["to_address"], "source": o["contact_source"], "thread_id": None}),
                             "make it shorter", {"subject": o["subject"], "body": o["body"]})
    assert "make it shorter" in revised["body"]


def test_candidates_are_filtered_before_any_research(conn):
    names = [a["company"] for a in outreach.candidates(conn, today=TODAY)]
    # Northwind: recruiter emailed. Greenfield: applied 19 days ago. Pixelworks: only 3 days, so not yet.
    assert set(names) == {"Greenfield Energy", "Northwind Robotics"}


def test_contact_lookup_is_cached_for_30_days_and_separate_from_research(conn):
    assert research.cached_contact(conn, "Greenfield Energy") is None
    found = research.fetch_contact("Greenfield Energy")
    assert found == {"contact_email": "recruiting@greenfield-energy.example",
                     "contact_url": "https://greenfield-energy.example/careers"}
    research.save_contact(conn, "Greenfield Energy", found)
    assert research.cached_contact(conn, "Greenfield Energy") == found
    assert research.cached(conn, "Greenfield Energy") is None  # a contact lookup is not full research
    conn.execute("UPDATE company_research SET contact_checked_at = datetime('now', '-31 days')")
    assert research.cached_contact(conn, "Greenfield Energy") is None


def test_no_contact_found_is_remembered(conn):
    research.save_contact(conn, "Pixelworks", {"contact_email": None, "contact_url": None})
    assert research.cached_contact(conn, "Pixelworks") == {"contact_email": None, "contact_url": None}
