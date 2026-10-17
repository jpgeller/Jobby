from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
import db
import main
import pipeline
import qa

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for i in ["fixture-01", "fixture-02", "fixture-03", "fixture-05", "fixture-06"]:
        pipeline.process_email(conn, emails[i])
    return conn


def test_context_has_applications_and_events(conn):
    ctx = qa.build_context(conn, now=NOW)
    assert ctx.startswith("Now: Monday 2026-10-19 09:00")
    assert "- Quanta Labs | Machine Learning Research Intern, Summer 2027 | offer" in ctx
    assert "Fri Oct 23, 3:00 PM | Northwind Robotics | interview" in ctx
    assert "(upcoming)" in ctx


def test_week_question_in_mock_mode_lists_events(conn):
    text = qa.answer(qa.build_context(conn, now=NOW), "what do I have this week?")
    assert text.startswith("[MOCK ANSWER") and "Northwind Robotics | interview" in text


def test_other_questions_in_mock_mode_list_applications(conn):
    text = qa.answer(qa.build_context(conn, now=NOW), "which companies rejected me?")
    assert "Helio Payments" in text
