import base64
import json
from datetime import datetime

import pytest

import classifier
import config
import gmail_watcher
import llm
import telegram_bot as bot


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


GMAIL_MESSAGE = {
    "id": "abc123", "threadId": "t1", "internalDate": "1792252800000", "snippet": "snippet text",
    "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "From", "value": "Priya <priya@northwind.example>"},
                    {"name": "Subject", "value": "[Jobby test] Interview invitation"}],
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>html version</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("plain version")}},
        ],
    },
}


def test_parse_message_prefers_plain_text():
    email = gmail_watcher.parse_message(GMAIL_MESSAGE)
    assert email["id"] == "abc123" and email["from"].startswith("Priya")
    assert email["body"] == "plain version"
    assert email["received"].startswith("2026-10-17")  # internalDate in Pacific time
    assert gmail_watcher.is_test_email(email)


def test_html_only_and_empty_bodies():
    html_only = json.loads(json.dumps(GMAIL_MESSAGE))
    html_only["payload"]["parts"] = html_only["payload"]["parts"][:1]
    assert gmail_watcher.parse_message(html_only)["body"] == "<p>html version</p>"
    no_parts = json.loads(json.dumps(GMAIL_MESSAGE))
    no_parts["payload"]["parts"] = []
    assert gmail_watcher.parse_message(no_parts)["body"] == "snippet text"


def test_mock_query_only_finds_test_emails(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    assert gmail_watcher.current_query().endswith('subject:"jobby test"')
    monkeypatch.setattr(config, "LLM_MODE", "live")
    assert gmail_watcher.current_query() == gmail_watcher.QUERY


def test_mock_test_email_gets_a_future_interview(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")
    email = gmail_watcher.parse_message(GMAIL_MESSAGE)
    email["body"] = "We'd like to invite you to interview."
    result = classifier.classify_email(email)
    start = datetime.fromisoformat(result["event"]["start"])
    assert result["category"] == "interview_invite"
    assert start > datetime.fromisoformat(email["received"])
    assert start.strftime("%a") == "Fri"  # shifted by whole weeks


def test_notification_text():
    c = {"category": "interview_invite", "company": "Northwind Robotics", "role": "SWE Intern",
         "event": {"start": "2026-10-30T15:00:00-07:00", "needs_scheduling": False},
         "next_step": "Technical interview with engineering team"}
    outcome = {"action": "updated", "old_status": "assessment", "new_status": "interview", "classification": c}
    assert bot.format_email_outcome(outcome, {"subject": "x"}) == (
        "Northwind Robotics moved to Interview - Fri Oct 30, 3:00 PM.\nNext: Technical interview with engineering team")
    assert bot.format_email_outcome({"action": "unchanged", "classification": c}, {"subject": "x"}) is None
    review = bot.format_email_outcome({"action": "needs_review"}, {"subject": "Quick question"})
    assert '"Quick question"' in review
