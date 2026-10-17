"""Reminders before events and the daily digest (spec Section 6C)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db

TZ = ZoneInfo(config.TIMEZONE)


def event_time(row):
    """When the event matters: a deadline's due time is its end; anything else, its start."""
    value = row["end"] if row["type"] == "deadline" and row["end"] else row["start"]
    return datetime.fromisoformat(value)


def due_reminders(conn, now=None):
    """[(event row, "24h" | "2h")] for future events that need a reminder now. An event first seen
    less than 2 hours out gets only the 2h reminder."""
    now = now or datetime.now(TZ)
    rows = conn.execute(
        "SELECT e.*, a.company, a.role, a.next_step FROM events e JOIN applications a ON a.id = e.application_id "
        "WHERE e.start IS NOT NULL AND a.status NOT IN ('rejected', 'withdrawn') "
        "AND (e.reminded_24h = 0 OR e.reminded_2h = 0)").fetchall()
    due = []
    for row in rows:
        left = event_time(row) - now
        if left <= timedelta(0):
            continue
        if left <= timedelta(hours=2) and not row["reminded_2h"]:
            due.append((row, "2h"))
        elif left <= timedelta(hours=24) and not row["reminded_24h"]:
            due.append((row, "24h"))
    return due


def mark_reminded(conn, row, kind):
    fields = {"reminded_2h": 1, "reminded_24h": 1} if kind == "2h" else {"reminded_24h": 1}
    db.update_event(conn, row["id"], **fields)
    conn.commit()


def format_reminder(row, kind):
    when = event_time(row).astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")
    soon = "in under 2 hours" if kind == "2h" else "coming up"
    if row["type"] == "deadline":
        what = row["title"].removeprefix("Deadline: ")
        text = f"Reminder: due {'in under 2 hours' if kind == '2h' else 'soon'} ({when}) - {what}."
    else:
        text = f"Reminder: {row['company']} {row['type']} {soon}, {when}."
        if row["location_or_link"]:
            text += f"\nLink: {row['location_or_link']}"
        if row["type"] == "interview":
            text += f"\nYour prep guide: /prep {row['company']}"
    return text


def daily_digest(conn, upcoming_text, now=None):
    """The 8am message: this week's events and open next steps. None if there is nothing to say."""
    now = now or datetime.now(TZ)
    steps = conn.execute(
        "SELECT company, next_step, next_deadline FROM applications "
        "WHERE next_step IS NOT NULL AND status NOT IN ('rejected', 'withdrawn') ORDER BY next_deadline IS NULL, next_deadline"
    ).fetchall()
    has_events = not upcoming_text.startswith("Nothing scheduled")
    if not has_events and not steps:
        return None
    lines = [f"Good morning! Here's your {now.strftime('%A')}."]
    if has_events:
        lines += ["", upcoming_text]
    if steps:
        lines += ["", "Open next steps:"] + [f"• {s['company']} - {s['next_step']}" for s in steps]
    return "\n".join(lines)
