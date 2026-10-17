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
