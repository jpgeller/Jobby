from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import db
import main
import pipeline
import prep
import reminders
import research
import telegram_bot as bot

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    monkeypatch.setattr(config, "PREP_DIR", tmp_path / "prep")


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for i in ["fixture-01", "fixture-02", "fixture-03"]:
        pipeline.process_email(conn, emails[i])
    return conn


INTERVIEW = datetime(2026, 10, 23, 15, 0, tzinfo=TZ)


def kinds(conn, now):
    return sorted((r["type"], k) for r, k in reminders.due_reminders(conn, now))


def test_24h_then_2h_then_nothing(conn):
    now = INTERVIEW - timedelta(hours=20)
    assert kinds(conn, now) == [("interview", "24h")]
    for row, kind in reminders.due_reminders(conn, now):
        reminders.mark_reminded(conn, row, kind)
    assert kinds(conn, now) == []
    now = INTERVIEW - timedelta(hours=1)
    assert kinds(conn, now) == [("interview", "2h")]
    for row, kind in reminders.due_reminders(conn, now):
        reminders.mark_reminded(conn, row, kind)
    assert kinds(conn, now) == []


def test_first_seen_inside_2h_gets_only_the_2h(conn):
    due = reminders.due_reminders(conn, INTERVIEW - timedelta(minutes=90))
    assert [(r["type"], k) for r, k in due] == [("interview", "2h")]
    reminders.mark_reminded(conn, *due[0])
    assert reminders.due_reminders(conn, INTERVIEW - timedelta(minutes=30)) == []


DUE_AT = datetime(2026, 10, 19, 23, 59, tzinfo=TZ)


def test_deadline_reminder_uses_due_time_and_its_own_title(tmp_path):
    conn = db.init_db(tmp_path / "oa.db")
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for i in ["fixture-01", "fixture-02"]:
        pipeline.process_email(conn, emails[i])
    due = reminders.due_reminders(conn, DUE_AT - timedelta(hours=5))
    assert [(r["type"], k) for r, k in due] == [("deadline", "24h")]
    assert reminders.format_reminder(*due[0]) == \
        "Reminder: due soon (Mon Oct 19, 11:59 PM) - Complete HackerRank OA - Northwind Robotics."


def test_moving_to_interview_silences_the_oa_deadline(conn):
    # conn has the interview email processed, so the OA is done.
    assert kinds(conn, DUE_AT - timedelta(hours=5)) == []


def test_no_reminders_for_past_or_rejected(conn):
    assert kinds(conn, INTERVIEW + timedelta(minutes=1)) == []
    conn.execute("UPDATE applications SET status = 'rejected'")
    assert kinds(conn, INTERVIEW - timedelta(hours=1)) == []


def test_interview_reminder_text(conn):
    row, kind = reminders.due_reminders(conn, INTERVIEW - timedelta(hours=1))[0]
    assert reminders.format_reminder(row, kind) == (
        "Reminder: Northwind Robotics interview in under 2 hours, Fri Oct 23, 3:00 PM.\n"
        "Link: https://zoom.example/j/8812345678\nYour prep guide: /prep Northwind Robotics")


def test_digest(conn):
    now = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
    text = reminders.daily_digest(conn, bot.format_upcoming(conn, now=now), now=now)
    assert text.startswith("Good morning! Here's your Monday.")
    assert "Fri Oct 23, 3:00 PM - Northwind Robotics interview" in text
    assert "Open next steps:\n• Northwind Robotics - Technical interview with engineering team" in text


def test_research_cache(conn):
    assert research.cached(conn, "Northwind Robotics") is None
    data = research.fetch("Northwind Robotics", "SWE Intern")
    assert data["summary"].startswith("[MOCK RESEARCH] Northwind Robotics") and data["sources"]
    research.save(conn, "Northwind Robotics, Inc.", data)
    assert research.cached(conn, "northwind robotics")["sources"] == data["sources"]
    conn.execute("UPDATE company_research SET researched_at = datetime('now', '-8 days')")
    assert research.cached(conn, "Northwind Robotics") is None


def test_prep_guide_has_every_section_and_is_saved(conn):
    app = db.list_applications(conn)[0]
    event = db.latest_event(conn, app["id"], "interview")
    data = research.fetch(app["company"], app["role"])
    text = prep.assemble(app, event, prep.write_guide(prep.build_prompt(app, event, data)), data)
    for section in ["1. What Northwind Robotics does", "4. Likely technical questions", "5. Likely behavioral",
                    "6. Questions to ask", "7. Logistics checklist", "- When: Fri Oct 23, 3:00 PM",
                    "- Where: https://zoom.example/j/8812345678", "Sources\n- Northwind Robotics - About"]:
        assert section in text
    path = prep.save_guide(app, event, text)
    assert path.name == "northwind-robotics_2026-10-23.md" and prep.latest_saved("Northwind Robotics") == path


def test_split_message():
    text = "\n\n".join(["x" * 1500] * 5)
    chunks = prep.split_message(text, limit=4000)
    assert all(len(c) <= 4000 for c in chunks) and "\n\n".join(chunks) == text
    assert prep.split_message("y" * 9000, limit=4000) == ["y" * 4000, "y" * 4000, "y" * 1000]


def test_new_interview_detection():
    assert bot.is_new_interview({"new_status": "interview", "old_status": "assessment", "event_id": 1})
    assert not bot.is_new_interview({"new_status": "interview", "old_status": "interview", "event_id": 1})
