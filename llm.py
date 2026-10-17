"""The only module that talks to Claude (spec Section 8).

LIVE mode calls the Anthropic API. MOCK mode returns canned output chosen by keywords in the
input, so every other module behaves exactly as it would live. Each call names a `task` so
mock mode knows which kind of canned answer to give.
"""
import base64
import copy
import json
import re
from datetime import datetime, timedelta

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


def ask_claude_research(prompt, *, task, system=None, model=None, max_searches=5, max_tokens=8000):
    """Prompt with the server-side web search tool. Returns (text, sources), where sources is a
    list of {"title", "url"} the answer cited (or, failing that, the search results)."""
    if config.LLM_MODE == "mock":
        return _mock_research(task, prompt)
    _ensure_client()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": model or config.MODEL_SMART, "max_tokens": max_tokens, "tools": tools}
    if system:
        kwargs["system"] = system
    blocks = []
    for _ in range(5):  # pause_turn: the server's search loop hit its limit; resend to resume
        response = _client.messages.create(messages=messages, **kwargs)
        blocks += response.content
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    if response.stop_reason == "refusal":
        raise LLMError(f"Claude declined the request ({response.stop_details})")
    text = "".join(b.text for b in blocks if b.type == "text")
    return text, _sources(blocks)


def _sources(blocks):
    cited, searched = {}, {}
    for b in blocks:
        if b.type == "text":
            for c in getattr(b, "citations", None) or []:
                if getattr(c, "url", None):
                    cited.setdefault(c.url, getattr(c, "title", None) or c.url)
        elif b.type == "web_search_tool_result" and isinstance(b.content, list):
            for r in b.content:
                searched.setdefault(r.url, r.title or r.url)
    chosen = cited or searched
    return [{"title": title, "url": url} for url, title in chosen.items()]


# ---------- live ----------

_client = None


def _ensure_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _live(content, system, model, schema, max_tokens):
    _ensure_client()
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


def _field(prompt, name):
    match = re.search(rf"^{name}: (.+)$", prompt, re.MULTILINE)
    return match.group(1).strip() if match else "the company"


def _mock_research(task, prompt):
    if task != "research_company":
        raise LLMError(f"No mock research for task {task!r}")
    company = _field(prompt, "Company")
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    text = (FIXTURES_DIR / "llm" / "research_mock.md").read_text().format(company=company, slug=slug)
    sources = [{"title": f"{company} - About", "url": f"https://{slug}.example/about"},
               {"title": f"{company} engineering blog", "url": f"https://{slug}.example/blog"}]
    return text, sources


def _canned_classifications():
    """{category: (canned response, received datetime of the fixture it came from)}"""
    canned = {}
    for path in sorted((FIXTURES_DIR / "emails").glob("*.json")):
        fixture = json.loads(path.read_text())
        response = fixture["mock_response"]
        canned.setdefault(response["category"], (response, datetime.fromisoformat(fixture["received"])))
    return canned


def _shift_dates(response, days):
    """Move event and deadline datetimes forward by whole days."""
    if days <= 0:
        return response
    shift = lambda v: (datetime.fromisoformat(v) + timedelta(days=days)).isoformat() if v else v
    response = copy.deepcopy(response)
    response["deadline"] = shift(response["deadline"])
    if response["event"]:
        response["event"]["start"] = shift(response["event"]["start"])
        response["event"]["end"] = shift(response["event"]["end"])
    return response


def _mock(task, prompt):
    if task == "classify_email":
        response, fixture_received = _canned_classifications()[mock_classify_category(prompt)]
        # A test email sent today gets the fixture's dates moved forward so a demo interview is
        # always in the future. Two days less than the full gap: the fixture interview is 8 days
        # after its email, and this lands it 6 days out, inside /upcoming's 7-day window.
        # Replaying the fixtures themselves shifts nothing.
        match = re.search(r"^Received: (\S+)", prompt, re.MULTILINE)
        if match:
            days = (datetime.fromisoformat(match.group(1)) - fixture_received).days
            response = _shift_dates(response, days - 2)
        return json.dumps(response)
    if task == "parse_screenshot":
        return (FIXTURES_DIR / "screenshots" / "handshake_mock.json").read_text()
    if task in ("write_outreach", "revise_outreach"):
        company, role = _field(prompt, "Company"), _field(prompt, "Role")
        body = (f"Hi,\n\nI applied for the {role} role at {company} and wanted to share why I'm excited about it. "
                f"[MOCK: one specific reason from the research goes here.] I recently built Jobby, an agent "
                f"that tracks internship applications end to end, and I'd love to bring that kind of work to "
                f"your team.\n\nThank you for your time!\nJoão")
        if task == "revise_outreach":
            change = prompt.rsplit("instructions from the student:", 1)[-1].strip()
            body += f"\n\n[MOCK REVISION - asked for: {change}]"
        return json.dumps({"subject": f"{role} application - João", "body": body})
    if task == "write_prep":
        company, role = _field(prompt, "Company"), _field(prompt, "Role")
        return (FIXTURES_DIR / "llm" / "prep_mock.md").read_text().format(company=company, role=role)
    raise LLMError(f"No mock response for task {task!r}; add one in llm._mock")
