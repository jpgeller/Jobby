from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import config
import db
import main
import pipeline
import telegram_bot as bot

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_CHAT_ID", "")


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    for email in sorted(main.load_fixture_emails(), key=lambda e: e["received"]):
        pipeline.process_email(conn, email)
    return conn


def test_first_start_registers_and_strangers_are_ignored(conn):
    assert bot.register_chat(conn, 111).startswith("Hi!")
    assert bot.allowed_chat_id(conn) == "111"
    assert bot.register_chat(conn, 222) is None
    assert bot.allowed_chat_id(conn) == "111"


def test_env_chat_id_wins(conn, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALLOWED_CHAT_ID", "999")
    assert bot.register_chat(conn, 111) is None
    assert bot.register_chat(conn, 999).startswith("Hi!")


def test_status(conn):
    text = bot.format_status(conn)
    assert text.startswith("3 applications: 1 interview, 1 offer, 1 rejected")
    assert "Quanta Labs - Sign and return offer letter (due Fri Oct 30, 11:59 PM)" in text
    assert "Helio" not in text  # rejected, no open step


def test_upcoming(conn):
    text = bot.format_upcoming(conn, now=NOW)
    assert "Mon Oct 26, 11:00 AM - Northwind Robotics interview" in text
    assert "Nothing scheduled" in bot.format_upcoming(conn, now=datetime(2026, 11, 1, tzinfo=NOW.tzinfo))


def test_add(conn):
    assert bot.add_application(conn, "Atlas Maps | Software Intern", today=date(2026, 10, 17)) == \
        "Added Atlas Maps - Software Intern as applied."
    assert bot.add_application(conn, "atlas maps | Software Intern").startswith("Already tracking")
    assert bot.add_application(conn, "Atlas Maps").startswith("Usage")


def test_update(conn):
    assert bot.update_status(conn, "Northwind Robotics rejected") == \
        "Northwind Robotics - Software Engineering Intern, Summer 2027: interview -> rejected."
    assert "No application found" in bot.update_status(conn, "Nowhere Inc applied")
    assert bot.update_status(conn, "Northwind maybe").startswith("Usage")


def test_update_asks_which_role_when_ambiguous(conn):
    bot.add_application(conn, "Quanta Labs | Data Engineering Intern")
    assert "several applications" in bot.update_status(conn, "Quanta Labs withdrawn")
    assert bot.update_status(conn, "Quanta Labs | Data Engineering Intern withdrawn").endswith("applied -> withdrawn.")
