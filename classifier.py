"""Turn one email into structured JSON (spec Section 5)."""
import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import llm

CATEGORIES = [
    "application_confirmation", "assessment_invite", "interview_invite", "interview_reschedule",
    "rejection", "offer", "recruiter_outreach", "not_relevant",
]
MIN_CONFIDENCE = 0.7
MAX_EMAIL_CHARS = 6000

_NULLABLE_STRING = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "number"},
        "company": _NULLABLE_STRING,
        "role": _NULLABLE_STRING,
        "event": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["interview", "assessment", "deadline", "other"]},
                        "start": _NULLABLE_STRING,
                        "end": _NULLABLE_STRING,
                        "location_or_link": _NULLABLE_STRING,
                        "needs_scheduling": {"type": "boolean"},
                    },
                    "required": ["type", "start", "end", "location_or_link", "needs_scheduling"],
                    "additionalProperties": False,
                },
            ]
        },
        "deadline": _NULLABLE_STRING,
        "next_step": _NULLABLE_STRING,
        "summary": {"type": "string"},
    },
    "required": ["category", "confidence", "company", "role", "event", "deadline", "next_step", "summary"],
    "additionalProperties": False,
}

SYSTEM = f"""You read emails from a student's inbox and decide whether each one is about an internship application.

Categories:
- application_confirmation: the application was received or submitted
- assessment_invite: an online assessment, coding challenge, or take-home to complete
- interview_invite: an interview is scheduled, or the student is asked to book one
- interview_reschedule: an existing interview moved to a new time
- rejection: the company will not move forward
- offer: the student received an offer
- recruiter_outreach: a recruiter reached out first about a role
- not_relevant: anything else, including job-board newsletters and recommendations

Rules:
- confidence is 0-1: how sure you are of the category.
- company is the hiring company (never "Handshake" or "Interstride"). role is the exact job title. Use null if unknown.
- Resolve relative dates ("next Tuesday") using the received date. A date written without a year is its next occurrence on or after the received date, almost always the same year; if a weekday is given, check it matches the year you chose. Write datetimes as ISO 8601 with UTC offset, in the {config.TIMEZONE} timezone unless the email names another.
- event: set it for interviews and assessments with a scheduled time. If the email asks the student to book a slot but gives no time, set needs_scheduling=true, start/end null, and next_step "Book interview slot". Otherwise null.
- deadline: the due date for an assessment, offer response, or similar; else null.
- next_step: a short action for the student, e.g. "Complete HackerRank OA"; null if none.
- summary: one short line, e.g. "45-min technical interview, Fri 3pm PT"."""


def email_to_text(body):
    """Strip HTML to plain text and cap the length sent to the model."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:MAX_EMAIL_CHARS]


def classify_email(email):
    """`email` has from, subject, received (ISO datetime), body. Returns the Section 5 JSON
    plus `needs_review`, true when Jobby should ask on Telegram instead of guessing."""
    prompt = (
        f"From: {email['from']}\n"
        f"Subject: {email['subject']}\n"
        f"Received: {email['received']} (student's timezone: {config.TIMEZONE})\n\n"
        f"{email_to_text(email['body'])}"
    )
    result = llm.ask_claude_json(prompt, task="classify_email", schema=SCHEMA, system=SYSTEM,
                                 model=config.MODEL_FAST, max_tokens=2048)
    if result.get("category") not in CATEGORIES:
        raise llm.LLMError(f"Unknown category {result.get('category')!r}")
    _fix_years(result, email["received"])
    relevant = result["category"] != "not_relevant"
    # A missing role is fine here: the pipeline matches on company alone when only one
    # application exists there, and asks you otherwise.
    result["needs_review"] = relevant and (result["confidence"] < MIN_CONFIDENCE or not result["company"])
    return result


def _fix_years(result, received):
    """Make every date safe to use: a missing time zone means the user's, a bare date means 11:59 PM
    that day, and a date 300+ days after the email almost always means the model picked the wrong
    year for a date written without one."""
    tz = ZoneInfo(config.TIMEZONE)
    received_at = datetime.fromisoformat(received)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=tz)

    def fix(value):
        if not value:
            return value
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None  # unparseable: better no date than a wrong one
        if "T" not in value:
            dt = dt.replace(hour=23, minute=59)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        value = dt.isoformat()
        if dt - received_at > timedelta(days=300):
            earlier = dt.replace(year=dt.year - 1)
            if earlier >= received_at - timedelta(days=1):
                return earlier.isoformat()
        return value

    result["deadline"] = fix(result["deadline"])
    if result["event"]:
        result["event"]["start"] = fix(result["event"]["start"])
        result["event"]["end"] = fix(result["event"]["end"])
