"""Telegram chat interface (spec Section 7). Polling mode, so no server is needed.

Each command handler is a thin wrapper around a plain function that returns the reply text,
so the logic can be tested without Telegram."""
import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import calendar_sync
import config
import db
import dedup
import gmail_watcher
import importer
import pipeline
import trackers

log = logging.getLogger(__name__)
TZ = ZoneInfo(config.TIMEZONE)
STATUSES = ["applied", "assessment", "interview", "offer", "rejected", "withdrawn"]
CLOSED = {"rejected", "withdrawn"}

HELP = """Commands:
/status - applications by status and open next steps
/upcoming - events in the next 7 days
/add Company | Role - add an application
/update Company status - change a status, e.g. /update Northwind rejected
/import - add applications from Handshake screenshots
/check - check the inbox for new application emails now"""


# ---------- who may talk to the bot ----------

def allowed_chat_id(conn):
    """The one chat Jobby answers: .env wins, else the chat that sent the first /start."""
    return config.TELEGRAM_ALLOWED_CHAT_ID or db.get_setting(conn, "telegram_chat_id")


def register_chat(conn, chat_id):
    """Handle /start. Returns the reply, or None to stay silent for a stranger."""
    owner = allowed_chat_id(conn)
    if owner and owner != str(chat_id):
        return None
    if not owner:
        db.set_setting(conn, "telegram_chat_id", str(chat_id))
    return f"Hi! Jobby is tracking your internship applications. Only this chat can use me now.\n\n{HELP}"


# ---------- command logic ----------

def format_status(conn):
    apps = db.list_applications(conn)
    if not apps:
        return "No applications yet. Add one with /add Company | Role."
    counts = Counter(a["status"] for a in apps)
    lines = [f"{len(apps)} applications: " + ", ".join(f"{counts[s]} {s}" for s in STATUSES if counts[s])]
    open_steps = [a for a in apps if a["next_step"] and a["status"] not in CLOSED]
    if open_steps:
        lines.append("\nNext steps:")
        for a in open_steps:
            due = f" (due {_fmt_time(a['next_deadline'])})" if a["next_deadline"] else ""
            lines.append(f"• {a['company']} - {a['next_step']}{due}")
    url = trackers.tracker_url(conn)
    if url:
        lines.append(f"\nTracker: {url}")
    return "\n".join(lines)


def format_upcoming(conn, now=None, days=7):
    now = now or datetime.now(TZ)
    rows = conn.execute(
        "SELECT e.*, a.company FROM events e JOIN applications a ON a.id = e.application_id "
        "WHERE e.start IS NOT NULL")
    upcoming = sorted(
        (datetime.fromisoformat(r["start"]), r) for r in rows
        if now <= datetime.fromisoformat(r["start"]) <= now + timedelta(days=days)
    )
    if not upcoming:
        return f"Nothing scheduled in the next {days} days."
    lines = [f"Next {days} days:"]
    for start, r in upcoming:
        link = f"\n  {r['location_or_link']}" if r["location_or_link"] else ""
        lines.append(f"• {_fmt_time(start)} - {r['company']} {r['type']}{link}")
    return "\n".join(lines)


def add_application(conn, args_text, today=None):
    company, sep, role = (args_text or "").partition("|")
    company, role = company.strip(), role.strip()
    if not sep or not company or not role:
        return "Usage: /add Company | Role\nExample: /add Northwind Robotics | Software Engineering Intern"
    existing = dedup.find_application(conn, company, role)
    if existing:
        return f"Already tracking {existing['company']} - {existing['role']} ({existing['status']})."
    db.insert_application(conn, company=company, company_norm=dedup.normalize_company(company),
                          role=role, source="other", status="applied",
                          applied_date=(today or date.today()).isoformat())
    conn.commit()
    return f"Added {company} - {role} as applied."


def update_status(conn, args_text):
    """`/update Company status` or `/update Company | Role status`. Manual updates may move a
    status in any direction, unlike emails."""
    words = (args_text or "").split()
    if len(words) < 2 or words[-1].lower() not in STATUSES:
        return f"Usage: /update Company status\nStatus is one of: {', '.join(STATUSES)}"
    status = words[-1].lower()
    company, _, role = " ".join(words[:-1]).partition("|")
    company, role = company.strip(), role.strip()

    if role:
        matches = [dedup.find_application(conn, company, role)]
        matches = [m for m in matches if m]
    else:
        norm = dedup.normalize_company(company)
        matches = [a for a in db.list_applications(conn) if a["company_norm"] == norm]
        if not matches:
            match = dedup.find_application(conn, company, None)
            matches = [match] if match else []

    if not matches:
        return f"No application found for {company}{' - ' + role if role else ''}."
    if len(matches) > 1:
        roles = "\n".join(f"• {m['role']}" for m in matches)
        return f"{company} has several applications. Say which one:\n{roles}\n\nExample: /update {company} | {matches[0]['role']} {status}"

    app = matches[0]
    if app["status"] == status:
        return f"{app['company']} - {app['role']} is already {status}."
    fields = {"status": status}
    if status in CLOSED:
        fields.update(next_step=None, next_deadline=None)
    db.update_application(conn, app["id"], **fields)
    conn.commit()
    return f"{app['company']} - {app['role']}: {app['status']} -> {status}."


def format_email_outcome(outcome, email):
    """The Telegram message for one processed email, or None when there is nothing to say."""
    action = outcome["action"]
    c = outcome.get("classification") or {}
    if action == "needs_review":
        return (f"I couldn't tell what this email is about:\n\"{email['subject']}\"\n"
                "If it matters, use /add or /update.")
    if action == "recruiter_lead":
        return f"A recruiter at {c['company']} reached out about {c['role']}: {c['summary']}"
    if action not in {"created", "updated"}:
        return None
    event = c.get("event") or {}
    when = f" - {_fmt_time(event['start'])}" if event.get("start") else ""
    old, new = outcome["old_status"], outcome["new_status"]
    if action == "created":
        text = f"New application tracked: {c['company']} - {c['role']} ({new}){when}."
    elif old != new:
        text = f"{c['company']} moved to {new.capitalize()}{when}."
    elif c["category"] == "interview_reschedule":
        text = f"{c['company']} interview rescheduled{when}."
    else:
        text = f"{c['company']} updated{when}."
    if event.get("needs_scheduling"):
        text += " You still need to book a slot."
    if c.get("next_step"):
        text += f"\nNext: {c['next_step']}"
    return text


def _fmt_time(value):
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p")


# ---------- Telegram wiring ----------

def build_app(conn, tracker=None, adapter=None):
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    sync_lock = asyncio.Lock()  # a timer sync and a command sync must not both append rows

    async def sync_tracker(context=None):
        """Mirror the database to the tracker sheet and the calendar."""
        async with sync_lock:
            if tracker is not None:
                try:
                    rows = trackers.rows_to_sync(conn)
                    row_ids = await asyncio.to_thread(tracker.sync, rows)
                    trackers.save_row_ids(conn, row_ids)
                except Exception:
                    log.exception("Tracker sync failed; will retry next cycle")
            if adapter is not None:
                work = calendar_sync.plan(conn)
                if work:
                    results = await asyncio.to_thread(calendar_sync.execute, adapter, work)
                    calendar_sync.save(conn, results)

    app.job_queue.run_repeating(sync_tracker, interval=config.POLL_MINUTES * 60, first=1)

    inbox_lock = asyncio.Lock()

    async def check_inbox(context):
        """Flow B: fetch new emails, process them oldest first, notify, sync the tracker.
        Returns how many emails were processed."""
        if inbox_lock.locked():
            return 0
        async with inbox_lock:
            try:
                gmail = await asyncio.to_thread(gmail_watcher.gmail_service)
                ids = await asyncio.to_thread(gmail_watcher.list_message_ids, gmail, gmail_watcher.current_query())
                new_ids = [i for i in ids if not db.is_email_processed(conn, i)]
                emails = [await asyncio.to_thread(gmail_watcher.get_email, gmail, i) for i in new_ids]
            except Exception:
                log.exception("Inbox check failed; will retry next cycle")
                return 0
            if config.LLM_MODE == "mock":  # the query already narrows to test emails; double-check
                emails = [e for e in emails if gmail_watcher.is_test_email(e)]
            count = 0
            for email in sorted(emails, key=lambda e: e["received"]):
                try:
                    outcome = pipeline.process_email(conn, email)
                except Exception:
                    log.exception("Could not process email %s", email["id"])
                    continue
                count += 1
                text = format_email_outcome(outcome, email)
                chat = allowed_chat_id(conn)
                if text and chat:
                    await context.bot.send_message(chat_id=chat, text=text, disable_web_page_preview=True)
                await asyncio.sleep(0)  # let other handlers run between emails
        if count:
            await sync_tracker()
        return count

    app.job_queue.run_repeating(check_inbox, interval=config.POLL_MINUTES * 60, first=10)

    async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        await reply(update, "Checking your inbox...")
        count = await check_inbox(context)
        if not count:
            await reply(update, "No new application emails.")

    async def reply(update, text):
        if text:
            await update.effective_message.reply_text(text, disable_web_page_preview=True)

    def owner_only(fn, sync=False):
        """Answer only the owner. sync=True mirrors the tracker right after a change."""
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if str(update.effective_chat.id) != allowed_chat_id(conn):
                log.info("Ignoring message from chat %s", update.effective_chat.id)
                return
            await reply(update, fn(update, context))
            if sync:
                context.application.create_task(sync_tracker())
        return handler

    def args_text(update):
        # Everything after the command, with spacing and "|" kept intact.
        text = update.effective_message.text or ""
        return text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await reply(update, register_chat(conn, update.effective_chat.id))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", owner_only(lambda u, c: format_status(conn))))
    app.add_handler(CommandHandler("upcoming", owner_only(lambda u, c: format_upcoming(conn))))
    app.add_handler(CommandHandler("add", owner_only(lambda u, c: add_application(conn, args_text(u)), sync=True)))
    app.add_handler(CommandHandler("update", owner_only(lambda u, c: update_status(conn, args_text(u)), sync=True)))
    app.add_handler(CommandHandler("help", owner_only(lambda u, c: HELP)))
    app.add_handler(CommandHandler("check", check_now))

    # ----- Flow A: screenshot import -----
    def start_import(update, context):
        context.chat_data["import"] = importer.ImportSession()
        return ("Send screenshots of your Handshake Applications page (one or more). "
                "Tip: send them as files, not photos, so Telegram doesn't blur the text.")

    async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        session = context.chat_data.get("import")
        if session is None:
            await reply(update, "To import applications from a screenshot, send /import first.")
            return
        msg = update.effective_message
        if msg.photo:
            file, media_type = await msg.photo[-1].get_file(), "image/jpeg"
        else:
            file, media_type = await msg.document.get_file(), msg.document.mime_type
        data = bytes(await file.download_as_bytearray())
        await reply(update, "Reading the screenshot...")
        try:
            found = await asyncio.to_thread(importer.parse_screenshot, data, media_type)
        except Exception:
            log.exception("Screenshot parsing failed")
            await reply(update, "I couldn't read that screenshot. Try again, or send it as a file.")
            return
        session.add(found)
        await reply(update, session.preview(conn))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        session = context.chat_data.get("import")
        if session is None:
            await reply(update, HELP)  # free-text Q&A arrives in milestone 13
            return
        text, done = importer.handle_reply(conn, session, update.effective_message.text)
        if done:
            context.chat_data.pop("import", None)
        await reply(update, text)
        if done:
            context.application.create_task(sync_tracker())

    async def owner_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) == allowed_chat_id(conn):
            await on_text(update, context)

    app.add_handler(CommandHandler("import", owner_only(start_import)))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, owner_gate))
    app.add_handler(MessageHandler(filters.ALL, owner_only(lambda u, c: HELP)))
    return app


def run(conn, tracker=None, adapter=None):
    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env, so the bot is not running.")
        return
    print("Telegram bot running. Press Ctrl+C to stop.")
    build_app(conn, tracker, adapter).run_polling()
