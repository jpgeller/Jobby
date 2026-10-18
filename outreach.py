"""Flow E (spec Section 6): suggest a short outreach email, ask on Telegram, send only after Approve.

Safety rules enforced here, not just in prompts:
- Nothing is sent unless the row was approved from the owner's chat (approve() + send_request()).
- At most 1 sent email per application and OUTREACH_MAX_PER_WEEK per 7 days.
- Contacts only from: a recruiter who emailed you, an address the company published (with its
  URL), or one you gave with /outreach. Never guessed.
- While OUTREACH_SEND_TO_SELF is on (the default), approved emails go to your own address."""
import re
from datetime import date, datetime, timezone
from email.utils import parseaddr

import config
import db
import llm

NOREPLY = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|@joinhandshake|@handshake|interstride",
                     re.I)
MAX_WORDS = 120
NO_REPLY_DAYS = 7
PROFILE_FILE = config.ROOT / "profile.md"

SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

SYSTEM = f"""You draft short outreach emails from a student to a company's recruiting contact, about an internship they applied to.

Rules:
- Under {MAX_WORDS} words in the body. Plain text, no markdown.
- Mention the exact role title.
- Give one specific reason for interest, taken from the research provided (a product, a team, or recent news). Never invent facts.
- Mention one relevant project from the student's profile, in one sentence.
- Polite and direct; no flattery, no generic templates, no attachments mentioned.
- If replying to a recruiter who wrote first, respond to them naturally.
- Sign with the student's first name from the profile.
- subject: short and specific, e.g. "Software Engineering Intern application - <name>"."""


# ---------- contacts and eligibility ----------

def recruiter_contact(conn, app_id):
    """(address, thread_id) of the latest real person who emailed about this application, else None."""
    rows = conn.execute("SELECT from_address, thread_id FROM processed_emails WHERE application_id = ? "
                        "AND from_address IS NOT NULL ORDER BY processed_at DESC", (app_id,)).fetchall()
    for row in rows:
        address = parseaddr(row["from_address"])[1]
        if address and not NOREPLY.search(address):
            return address, row["thread_id"]
    return None


def sent_this_week(conn):
    return conn.execute("SELECT count(*) FROM outreach WHERE status = 'sent' "
                        "AND sent_at >= datetime('now', '-7 days')").fetchone()[0]


def suggested_this_week(conn):
    return conn.execute("SELECT count(*) FROM outreach WHERE status IN ('suggested', 'approved', 'sent') "
                        "AND created_at >= datetime('now', '-7 days')").fetchone()[0]


def history(conn, app_id):
    """Statuses of past outreach rows for an application."""
    return {r["status"] for r in conn.execute("SELECT status FROM outreach WHERE application_id = ?", (app_id,))}


def candidates(conn, today=None):
    """Applications the daily job may consider: still waiting (applied/assessment), never sent,
    never skipped, no draft pending, and already eligible under a contact rule (a recruiter emailed,
    or applied 7+ days ago with no reply). Filtering here, before any research, keeps the daily job
    from paying to research applications that couldn't get outreach anyway."""
    today = today or date.today()
    apps = conn.execute("SELECT * FROM applications WHERE status IN ('applied', 'assessment') ORDER BY applied_date")
    eligible = []
    for a in apps:
        if history(conn, a["id"]) & {"sent", "skipped", "suggested", "approved"}:
            continue
        waited = (a["status"] == "applied" and a["applied_date"]
                  and (today - date.fromisoformat(a["applied_date"])).days >= NO_REPLY_DAYS)
        if waited or recruiter_contact(conn, a["id"]):
            eligible.append(a)
    return eligible


def pick_contact(conn, app, research_data, today=None, manual=False):
    """Who to write to and why, or None. Returns dict(to, source, thread_id, reason)."""
    recruiter = recruiter_contact(conn, app["id"])
    if recruiter:
        return {"to": recruiter[0], "source": "recruiter_email", "thread_id": recruiter[1],
                "reason": "A recruiter emailed you about this application; a short reply keeps you on their radar."}
    if not (research_data and research_data.get("contact_email")):
        return None
    days = (today or date.today()) - date.fromisoformat(app["applied_date"]) if app["applied_date"] else None
    waiting = app["status"] == "applied" and days is not None and days.days >= NO_REPLY_DAYS
    if not (waiting or manual):
        return None
    reason = (f"You applied {days.days} days ago and haven't heard back." if waiting
              else "You asked for an outreach draft.")
    return {"to": research_data["contact_email"], "source": f"published at {research_data['contact_url']}",
            "thread_id": None, "reason": reason}


# ---------- drafting ----------

def build_prompt(app, research_data, contact):
    profile = PROFILE_FILE.read_text() if PROFILE_FILE.exists() else "(no profile provided)"
    return (
        f"Company: {app['company']}\n"
        f"Role: {app['role']}\n"
        f"Applied: {app['applied_date'] or 'unknown'}; current stage: {app['status']}\n"
        f"Writing to: {contact['to']} ({_contact_note(contact)})\n\n"
        f"Research:\n{research_data['summary'] if research_data else '-'}\n\n"
        f"Recent news:\n{(research_data or {}).get('recent_news') or '-'}\n\n"
        f"Student profile:\n{profile}"
    )


def _contact_note(contact):
    if contact["source"] == "recruiter_email":
        return "replying to a recruiter who emailed first"
    return contact["source"]


def draft(prompt, instructions=None, previous=None):
    """Worker-thread safe. instructions + previous = a revision after you tapped Edit."""
    task = "write_outreach"
    if instructions:
        task = "revise_outreach"
        prompt += (f"\n\nPrevious draft:\nSubject: {previous['subject']}\n{previous['body']}\n\n"
                   f"Revise it following these instructions from the student: {instructions}")
    result = llm.ask_claude_json(prompt, task=task, schema=SCHEMA, system=SYSTEM, model=config.MODEL_SMART)
    result["body"] = result["body"].strip()
    return result


def word_count(text):
    return len(text.split())


# ---------- the outreach row's life ----------

def save_suggestion(conn, app_id, contact, email):
    cur = conn.execute(
        "INSERT INTO outreach (application_id, to_address, contact_source, subject, body, status, thread_id, reason) "
        "VALUES (?, ?, ?, ?, ?, 'suggested', ?, ?)",
        (app_id, contact["to"], contact["source"], email["subject"], email["body"], contact["thread_id"],
         contact["reason"]))
    conn.commit()
    return cur.lastrowid


def get(conn, outreach_id):
    return conn.execute("SELECT o.*, a.company, a.role FROM outreach o JOIN applications a ON a.id = o.application_id "
                        "WHERE o.id = ?", (outreach_id,)).fetchone()


def pending_for(conn, app_id):
    return conn.execute("SELECT id FROM outreach WHERE application_id = ? AND status = 'suggested' "
                        "ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()


def preview(conn, outreach_id):
    o = get(conn, outreach_id)
    test = "\n(Test mode: Approve sends this to your own address.)" if config.OUTREACH_SEND_TO_SELF else ""
    return (f"Jobby suggests emailing {o['to_address']} about {o['role']} at {o['company']}.\n"
            f"Why: {o['reason']}\nContact source: {o['contact_source']}{test}\n\n"
            f"Subject: {o['subject']}\n\n{o['body']}\n\n({word_count(o['body'])} words)")


def update_draft(conn, outreach_id, email):
    conn.execute("UPDATE outreach SET subject = ?, body = ? WHERE id = ? AND status = 'suggested'",
                 (email["subject"], email["body"], outreach_id))
    conn.commit()


def set_telegram_message(conn, outreach_id, message_id):
    conn.execute("UPDATE outreach SET telegram_message_id = ? WHERE id = ?", (str(message_id), outreach_id))
    conn.commit()


def approve(conn, outreach_id, chat_id):
    """The only way a row becomes sendable. Returns (ok, message)."""
    owner = config.TELEGRAM_ALLOWED_CHAT_ID or db.get_setting(conn, "telegram_chat_id")
    if not owner or str(chat_id) != owner:
        return False, "Only the owner's chat can approve outreach."
    o = get(conn, outreach_id)
    if o is None or o["status"] != "suggested":
        return False, "This draft was already handled."
    if "sent" in history(conn, o["application_id"]):
        return False, f"You already sent an outreach email for {o['company']} - {o['role']}."
    if sent_this_week(conn) >= config.OUTREACH_MAX_PER_WEEK:
        return False, f"Weekly limit reached ({config.OUTREACH_MAX_PER_WEEK} sent in the last 7 days)."
    # Conditional update: a double tap can't approve twice.
    cur = conn.execute("UPDATE outreach SET status = 'approved' WHERE id = ? AND status = 'suggested'", (outreach_id,))
    conn.commit()
    return cur.rowcount == 1, "Approved."


def send_request(conn, outreach_id, my_address=None):
    """kwargs for actions.call(adapter, "send_email", ...). Refuses anything not approved."""
    o = get(conn, outreach_id)
    if o is None or o["status"] != "approved":
        raise PermissionError(f"Outreach {outreach_id} is not approved; refusing to send")
    to, subject, body, thread = o["to_address"], o["subject"], o["body"], o["thread_id"]
    if config.OUTREACH_SEND_TO_SELF:
        # The tag keeps Jobby's own inbox check from picking this copy up.
        to, thread = my_address, None
        subject = f"[Jobby outreach test] {subject}"
        body = f"(Test send. In real mode this goes to {o['to_address']}.)\n\n{body}"
    return {"to": to, "subject": subject, "body": body, "reply_to_thread_id": thread}


def mark_sent(conn, outreach_id, sent_id, today=None):
    o = get(conn, outreach_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE outreach SET status = 'sent', gmail_sent_id = ?, sent_at = ? WHERE id = ?",
                 (sent_id, now, outreach_id))
    app = db.get_application(conn, o["application_id"])
    note = f"Outreach sent to {o['to_address']} on {(today or date.today()).isoformat()}"
    if config.OUTREACH_SEND_TO_SELF:
        note += " (test, to self)"
    db.update_application(conn, app["id"], notes=f"{app['notes']}\n{note}" if app["notes"] else note)
    conn.commit()


def mark_failed(conn, outreach_id):
    """Sending failed: back to suggested so Approve can be tapped again."""
    conn.execute("UPDATE outreach SET status = 'suggested' WHERE id = ? AND status = 'approved'", (outreach_id,))
    conn.commit()


def skip(conn, outreach_id):
    conn.execute("UPDATE outreach SET status = 'skipped' WHERE id = ? AND status = 'suggested'", (outreach_id,))
    conn.commit()
