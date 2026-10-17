from datetime import date

import pytest

import config
import db
import importer


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    db.insert_application(conn, company="Northwind Robotics", company_norm="northwind robotics",
                          role="Software Engineering Intern, Summer 2027", status="interview")
    return conn


def screenshot():
    return importer.parse_screenshot(b"fake-png", "image/png", today=date(2026, 10, 17))


def test_overlapping_screenshots_do_not_repeat_rows():
    session = importer.ImportSession()
    assert session.add(screenshot()) == 7
    assert session.add(screenshot()) == 0
    assert len(session.items) == 7


def test_preview_marks_already_tracked(conn):
    session = importer.ImportSession()
    session.add(screenshot())
    text = session.preview(conn)
    assert text.startswith("Found 7:")
    assert "1. Northwind Robotics - Software Engineering Intern, Summer 2027 (interview, 2026-10-03) (already tracked" in text


def test_remove_then_ok_imports_the_rest(conn):
    session = importer.ImportSession()
    session.add(screenshot())
    reply, done = importer.handle_reply(conn, session, "remove 2 9")
    assert not done and reply.startswith("No item 9.") and "Pixelworks" not in reply
    reply, done = importer.handle_reply(conn, session, "ok")
    assert done and reply == "Imported 5 applications. Skipped 1 already tracked."
    statuses = {a["company"]: a["status"] for a in db.list_applications(conn)}
    assert statuses["Atlas Maps"] == "assessment" and statuses["Corvid Security"] == "rejected"
    assert "Pixelworks" not in statuses
    assert db.list_applications(conn)[-1]["source"] == "handshake"


def test_cancel_adds_nothing(conn):
    session = importer.ImportSession()
    session.add(screenshot())
    reply, done = importer.handle_reply(conn, session, "cancel")
    assert done and len(db.list_applications(conn)) == 1


def test_unknown_reply_keeps_waiting(conn):
    reply, done = importer.handle_reply(conn, importer.ImportSession(), "hello")
    assert not done and "ok" in reply
