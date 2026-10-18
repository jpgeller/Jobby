import json

import pytest

import classifier
import config
import llm
import main


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


FIXTURES = main.load_fixture_emails()


def test_one_fixture_per_category():
    assert sorted(e["mock_response"]["category"] for e in FIXTURES) == sorted(classifier.CATEGORIES)


@pytest.mark.parametrize("email", FIXTURES, ids=[e["id"] for e in FIXTURES])
def test_mock_keywords_pick_the_right_category(email):
    text = f"{email['subject']}\n{classifier.email_to_text(email['body'])}"
    assert llm.mock_classify_category(text) == email["mock_response"]["category"]


@pytest.mark.parametrize("email", FIXTURES, ids=[e["id"] for e in FIXTURES])
def test_classify_email_returns_valid_result(email):
    result = classifier.classify_email(email)
    assert result["category"] == email["mock_response"]["category"]
    assert set(classifier.SCHEMA["required"]) <= result.keys()
    assert result["needs_review"] is False


def test_low_confidence_needs_review(monkeypatch):
    low = dict(FIXTURES[0]["mock_response"], confidence=0.4)
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(low))
    assert classifier.classify_email(FIXTURES[0])["needs_review"] is True


def test_email_to_text_strips_html_and_truncates():
    text = classifier.email_to_text("<p>Hi&nbsp;there</p><script>x()</script>" + "a" * 10000)
    assert text.startswith("Hi") and "<" not in text and "x()" not in text
    assert len(text) == classifier.MAX_EMAIL_CHARS


def test_wrong_year_is_pulled_back(monkeypatch):
    bad = dict(FIXTURES[2]["mock_response"])
    bad["event"] = dict(bad["event"], start="2027-10-23T15:00:00-07:00", end="2027-10-23T15:45:00-07:00")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(bad))
    result = classifier.classify_email(FIXTURES[2])
    assert result["event"]["start"] == "2026-10-23T15:00:00-07:00"


def test_missing_role_alone_does_not_need_review(monkeypatch):
    no_role = dict(FIXTURES[3]["mock_response"], role=None)
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(no_role))
    assert classifier.classify_email(FIXTURES[3])["needs_review"] is False


def test_naive_and_date_only_values_get_the_user_timezone(monkeypatch):
    odd = dict(FIXTURES[5]["mock_response"], deadline="2026-10-30")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(odd))
    assert classifier.classify_email(FIXTURES[5])["deadline"] == "2026-10-30T23:59:00-07:00"
    odd = dict(FIXTURES[5]["mock_response"], deadline="2026-10-30T17:00:00")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(odd))
    assert classifier.classify_email(FIXTURES[5])["deadline"] == "2026-10-30T17:00:00-07:00"
    odd = dict(FIXTURES[5]["mock_response"], deadline="next Friday")
    monkeypatch.setattr(llm, "ask_claude", lambda *a, **k: json.dumps(odd))
    assert classifier.classify_email(FIXTURES[5])["deadline"] is None
