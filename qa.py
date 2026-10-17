"""Free-text questions on Telegram, answered by Claude with the database as context (spec Section 7)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import llm
import reminders

TZ = ZoneInfo(config.TIMEZONE)

SYSTEM = """You are Jobby, a student's internship application assistant, answering on Telegram.
Answer only from the data provided; if it isn't there, say you don't know and suggest a command
(/status, /upcoming, /add, /update, /prep, /research, /outreach). Be brief: a few lines, plain text,
no markdown. Give dates as weekday and date, times in Pacific time."""


def build_context(conn, now=None):
    """Everything Jobby knows, as labeled plain-text sections. Main thread only."""
    now = now or datetime.now(TZ)
    apps = db.list_applications(conn)
    lines = [f"Now: {now.strftime('%A %Y-%m-%d %H:%M')} ({config.TIMEZONE})", "", "APPLICATIONS:"]
    for a in apps:
        due = f", due {_fmt(a['next_deadline'])}" if a["next_deadline"] else ""
        lines.append(f"- {a['company']} | {a['role']} | {a['status']} | applied {a['applied_date'] or '?'}"
                     f" | next: {a['next_step'] or '-'}{due}")
    lines += ["", "EVENTS (past 7 days to next 30 days):"]
    rows = conn.execute("SELECT e.*, a.company FROM events e JOIN applications a ON a.id = e.application_id "
                        "WHERE e.start IS NOT NULL").fetchall()
    events = sorted((reminders.event_time(r), r) for r in rows
                    if now - timedelta(days=7) <= reminders.event_time(r) <= now + timedelta(days=30))
    for when, r in events:
        tense = "past" if when < now else "upcoming"
        lines.append(f"- {_fmt(when)} | {r['company']} | {r['type']} | {r['title']} ({tense})")
    pending = conn.execute("SELECT a.company, o.to_address FROM outreach o JOIN applications a "
                           "ON a.id = o.application_id WHERE o.status = 'suggested'").fetchall()
    if pending:
        lines += ["", "OUTREACH DRAFTS WAITING FOR APPROVAL:"] + [f"- {p['company']} to {p['to_address']}" for p in pending]
    return "\n".join(lines)


def answer(context, question):
    """Worker-thread safe."""
    prompt = f"{context}\n\nQUESTION: {question}"
    return llm.ask_claude(prompt, task="answer_question", system=SYSTEM, model=config.MODEL_FAST,
                          max_tokens=2048).strip()


def _fmt(value):
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")
