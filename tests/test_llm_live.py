"""The live code path, with a fake Anthropic client: checks request shapes and reply parsing
without spending credits."""
import json
from types import SimpleNamespace as NS

import pytest

import classifier
import config
import llm


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "live")

    def install(*responses):
        messages = FakeMessages(responses)
        monkeypatch.setattr(llm, "_client", NS(messages=messages))
        return messages
    return install


def text_reply(text, stop="end_turn"):
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text, citations=None)],
              stop_reason=stop, stop_details=None)


def test_classify_sends_schema_and_parses_json(fake):
    answer = {"category": "rejection", "confidence": 0.9, "company": "Helio", "role": "DS Intern", "event": None,
              "deadline": None, "next_step": None, "summary": "Rejected"}
    messages = fake(text_reply(json.dumps(answer)))
    result = classifier.classify_email({"from": "a@b.c", "subject": "Update", "received": "2026-10-10T08:00:00-07:00",
                                        "body": "<p>Unfortunately...</p>"})
    req = messages.requests[0]
    assert req["model"] == config.MODEL_FAST and req["system"] == classifier.SYSTEM
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "Unfortunately..." in req["messages"][0]["content"][0]["text"]
    assert result["category"] == "rejection" and result["needs_review"] is False


def test_vision_sends_image_block(fake):
    messages = fake(text_reply('{"applications": []}'))
    llm.ask_claude_vision([(b"png-bytes", "image/png")], "List them", task="parse_screenshot", schema={"type": "object"})
    content = messages.requests[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "List them"}


def test_refusal_and_truncation_raise(fake):
    fake(NS(content=[], stop_reason="refusal", stop_details="cyber"))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.ask_claude("x", task="t")
    fake(text_reply("{", stop="max_tokens"))
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.ask_claude("x", task="t")


def test_research_resumes_pause_turn_and_collects_citations(fake):
    search = NS(type="web_search_tool_result", content=[NS(url="https://a.example", title="A"),
                                                        NS(url="https://b.example", title="B")])
    paused = NS(content=[NS(type="server_tool_use"), search], stop_reason="pause_turn", stop_details=None)
    cited = NS(type="text", text="SUMMARY: Acme makes rockets.",
               citations=[NS(url="https://b.example", title="B page")])
    done = NS(content=[cited], stop_reason="end_turn", stop_details=None)
    messages = fake(paused, done)
    text, sources = llm.ask_claude_research("Company: Acme", task="research_company")
    assert len(messages.requests) == 2
    assert messages.requests[0]["tools"][0]["type"] == "web_search_20260209"
    assert messages.requests[1]["messages"][1] == {"role": "assistant", "content": paused.content}
    assert text == "SUMMARY: Acme makes rockets."
    assert sources == [{"title": "B page", "url": "https://b.example"}]  # cited beats merely searched


def test_research_falls_back_to_search_results_without_citations(fake):
    search = NS(type="web_search_tool_result", content=[NS(url="https://a.example", title="A")])
    fake(NS(content=[search, NS(type="text", text="SUMMARY: x", citations=None)], stop_reason="end_turn",
            stop_details=None))
    assert llm.ask_claude_research("Company: Acme", task="research_company")[1] == [{"title": "A", "url": "https://a.example"}]
