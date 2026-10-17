"""Interview prep guide (spec Section 6D): sent on Telegram and saved to prep/<company>_<date>.md.

Claude writes the sections that need judgment; the logistics checklist and source links are
filled in by code from the database, so they are always exact."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import llm

TZ = ZoneInfo(config.TIMEZONE)
PROFILE_FILE = config.ROOT / "profile.md"
TELEGRAM_LIMIT = 4000

SYSTEM = """You write interview prep guides for a student applying to internships. Be specific to the company and role; no generic filler.

Write plain text (no markdown symbols like # or **), with these numbered sections:
1. What the company does - products and recent news, based only on the research provided.
2. What the role likely involves.
3. Likely interview format for this stage and company, and what to prepare.
4. Likely technical questions - 5, each with a one-line tip.
5. Likely behavioral questions - 5, with tips that point to specific projects from the student's profile when one fits.
6. Questions to ask the interviewer - 3 smart ones.

Do not write a logistics or sources section; those are added separately."""


def build_prompt(app, event, research):
    profile = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else "(no profile provided; give general tips)"
    when = _fmt(event["start"]) if event and event["start"] else "not scheduled yet"
    return (
        f"Company: {app['company']}\n"
        f"Role: {app['role']}\n"
        f"Interview: {event['type'] if event else 'interview'} at {when}\n"
        f"Next step from the email: {app['next_step'] or '-'}\n"
        f"Job posting: {app['job_url'] or '-'}\n\n"
        f"Research summary:\n{research['summary']}\n\n"
        f"Recent news:\n{research['recent_news'] or '-'}\n\n"
        f"Student profile:\n{profile}"
    )


def write_guide(prompt):
    """Worker-thread safe."""
    return llm.ask_claude(prompt, task="write_prep", system=SYSTEM, model=config.MODEL_SMART, max_tokens=8000).strip()


def assemble(app, event, body, research):
    lines = [f"Prep guide: {app['company']} - {app['role']}", "", body, "", "7. Logistics checklist"]
    if event and event["start"]:
        lines.append(f"- When: {_fmt(event['start'])} ({config.TIMEZONE})")
        if event["location_or_link"]:
            lines.append(f"- Where: {event['location_or_link']}")
    else:
        lines.append("- When: not scheduled yet - book a slot")
    lines += ["- Who: check the invite email for interviewer names",
              "- Test your camera, mic, and internet 15 minutes before",
              "- Have your resume and a notepad open"]
    if research["sources"]:
        lines += ["", "Sources"] + [f"- {s['title']}: {s['url']}" for s in research["sources"][:8]]
    return "\n".join(lines)


def save_guide(app, event, text):
    config.PREP_DIR.mkdir(exist_ok=True)
    day = (datetime.fromisoformat(event["start"]).astimezone(TZ) if event and event["start"]
           else datetime.now(TZ)).strftime("%Y-%m-%d")
    path = config.PREP_DIR / f"{_slug(app['company'])}_{day}.md"
    path.write_text(text)
    return path


def latest_saved(company):
    files = sorted(config.PREP_DIR.glob(f"{_slug(company)}_*.md")) if config.PREP_DIR.exists() else []
    return files[-1] if files else None


def split_message(text, limit=TELEGRAM_LIMIT):
    """Split on paragraph boundaries so each Telegram message stays under the limit."""
    chunks, current = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:  # one huge paragraph: hard split
            chunks.append(para[:limit])
            para = para[limit:]
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _slug(company):
    return re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")


def _fmt(value):
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")
