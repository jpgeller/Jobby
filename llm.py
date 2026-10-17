"""The only module that talks to Claude (spec Section 8).

LIVE mode calls the Anthropic API. MOCK mode returns canned output chosen by keywords in the
input, so every other module behaves exactly as it would live. Each call names a `task` so
mock mode knows which kind of canned answer to give.
"""
import base64
import json
import re

import config

FIXTURES_DIR = config.ROOT / "fixtures"


class LLMError(Exception):
    pass


# ---------- public API ----------

def ask_claude(prompt, *, task, system=None, model=None, schema=None, max_tokens=4096):
    """Send a text prompt; return the reply text (a JSON string when `schema` is given)."""
    if config.LLM_MODE == "mock":
        return _mock(task, prompt)
    return _live([{"type": "text", "text": prompt}], system, model, schema, max_tokens)


def ask_claude_json(prompt, *, task, schema, system=None, model=None, max_tokens=4096):
    """Like ask_claude, but the reply is forced to match `schema` and returned parsed."""
    text = ask_claude(prompt, task=task, system=system, model=model, schema=schema, max_tokens=max_tokens)
    return parse_json(text, task)


def parse_json(text, task):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"{task}: reply was not valid JSON: {text[:200]!r}") from e


def ask_claude_vision(images, prompt, *, task, system=None, model=None, schema=None, max_tokens=4096):
    """`images` is a list of (bytes, media_type) pairs, e.g. (png_bytes, "image/png")."""
    if config.LLM_MODE == "mock":
        return _mock(task, prompt)
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(data).decode("ascii")}}
        for data, media_type in images
    ]
    content.append({"type": "text", "text": prompt})
    return _live(content, system, model, schema, max_tokens)


# ---------- live ----------

_client = None


def _live(content, system, model, schema, max_tokens):
    global _client
    import anthropic

    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    kwargs = {
        "model": model or config.MODEL_FAST,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        kwargs["system"] = system
    if schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    response = _client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise LLMError(f"Claude declined the request ({response.stop_details})")
    if response.stop_reason == "max_tokens":
        raise LLMError("Reply was cut off at max_tokens")
    return "".join(b.text for b in response.content if b.type == "text")


# ---------- mock ----------

# Checked in order; the first category whose pattern matches the email wins.
_CLASSIFY_KEYWORDS = [
    ("interview_reschedule", r"reschedul|new time|moved to"),
    ("offer", r"pleased to offer|offer letter|offer of employment"),
    ("rejection", r"unfortunately|not to move forward|other candidates"),
    # Interview before assessment: invites often say "great work on the assessment".
    ("interview_invite", r"interview"),
    ("assessment_invite", r"assessment|hackerrank|codesignal|coding challenge"),
    ("application_confirmation", r"thank you for applying|received your application|application .{0,40}submitted"),
    ("recruiter_outreach", r"came across your profile|i'm a recruiter|recruiter at"),
]


def mock_classify_category(text):
    lowered = text.lower()
    for category, pattern in _CLASSIFY_KEYWORDS:
        if re.search(pattern, lowered):
            return category
    return "not_relevant"


def _canned_classifications():
    canned = {}
    for path in sorted((FIXTURES_DIR / "emails").glob("*.json")):
        response = json.loads(path.read_text())["mock_response"]
        canned.setdefault(response["category"], response)
    return canned


def _mock(task, prompt):
    if task == "classify_email":
        category = mock_classify_category(prompt)
        return json.dumps(_canned_classifications()[category])
    if task == "parse_screenshot":
        return (FIXTURES_DIR / "screenshots" / "handshake_mock.json").read_text()
    raise LLMError(f"No mock response for task {task!r}; add one in llm._mock")
