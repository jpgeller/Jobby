"""Telegram chat interface (spec Section 7). Polling mode, so no server is needed.

Each command handler is a thin wrapper around a plain function that returns the reply text,
so the logic can be tested without Telegram."""
import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import actions
import calendar_sync
import classifier
import config
import db
import dedup
import gmail_watcher
import google_auth
import importer
import outreach
import pipeline
import prep
import reminders
import research
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
/check - check the inbox for new application emails now
/prep Company - interview prep guide
/research Company - what Jobby knows about a company, with sources
/outreach Company [email] - draft an outreach email now (you still approve it)
/call Company - have Fo call you now with a quick prep summary for the next interview"""


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
        (reminders.event_time(r), r) for r in rows
        if now <= reminders.event_time(r) <= now + timedelta(days=days)
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


def find_by_name(conn, text):
    """`Company` or `Company | Role` -> (company, role, matching application rows)."""
    company, _, role = (text or "").partition("|")
    company, role = company.strip(), role.strip()
    if role:
        match = dedup.find_application(conn, company, role)
        return company, role, [match] if match else []
    norm = dedup.normalize_company(company)
    matches = [a for a in db.list_applications(conn) if a["company_norm"] == norm]
    if not matches:
        match = dedup.find_application(conn, company, None)
        matches = [match] if match else []
    return company, role, matches


def is_new_interview(outcome):
    return (outcome.get("new_status") == "interview" and outcome.get("old_status") != "interview"
            and outcome.get("event_id") is not None)


def update_status(conn, args_text):
    """`/update Company status` or `/update Company | Role status`. Manual updates may move a
    status in any direction, unlike emails."""
    words = (args_text or "").split()
    if len(words) < 2 or words[-1].lower() not in STATUSES:
        return f"Usage: /update Company status\nStatus is one of: {', '.join(STATUSES)}"
    status = words[-1].lower()
    company, role, matches = find_by_name(conn, " ".join(words[:-1]))

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
            count, new_interviews = 0, []
            for email in sorted(emails, key=lambda e: e["received"]):
                try:
                    # The Claude call runs in a worker thread so the bot keeps answering during a
                    # big first sync; the database writes stay here on the main thread.
                    result = await asyncio.to_thread(classifier.classify_email, email)
                    outcome = pipeline.process_email(conn, email, result)
                except Exception:
                    log.exception("Could not process email %s", email["id"])
                    continue
                count += 1
                text = format_email_outcome(outcome, email)
                if text and is_new_interview(outcome):
                    text += "\nPrep guide coming."
                    new_interviews.append((outcome["application_id"], outcome["event_id"]))
                await send(context.bot, text)
                await asyncio.sleep(0)  # let other handlers run between emails
        if count:
            await sync_tracker()
        for app_id, event_id in new_interviews:
            context.application.create_task(send_prep(context.bot, app_id, event_id))
        return count

    app.job_queue.run_repeating(check_inbox, interval=config.POLL_MINUTES * 60, first=10)

    async def send(bot, text):
        """Message the owner, split into Telegram-sized pieces."""
        chat = allowed_chat_id(conn)
        if not text or not chat:
            return
        for chunk in prep.split_message(text):
            await bot.send_message(chat_id=chat, text=chunk, disable_web_page_preview=True)

    async def get_research(company, role):
        data = research.cached(conn, company)
        if data is None:
            data = await asyncio.to_thread(research.fetch, company, role)
            research.save(conn, company, data)
        return data

    async def send_prep(bot, app_id, event_id=None):
        """Flow D: research the company, write the guide, save it, send it."""
        app_row = db.get_application(conn, app_id)
        event = (conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone() if event_id
                 else db.latest_event(conn, app_id, "interview"))
        try:
            data = await get_research(app_row["company"], app_row["role"])
            body = await asyncio.to_thread(prep.write_guide, prep.build_prompt(app_row, event, data))
        except Exception:
            log.exception("Prep guide failed for %s", app_row["company"])
            await send(bot, f"I couldn't write the prep guide for {app_row['company']}. Try /prep {app_row['company']} later.")
            return
        text = prep.assemble(app_row, event, body, data)
        prep.save_guide(app_row, event, text)
        if event:
            db.update_event(conn, event["id"], prep_sent=1)
            conn.commit()
        await send(bot, text)

    async def place_call(bot, row):
        """Flow C: Fo calls with a spoken summary; if that fails, the same summary goes to Telegram."""
        reminders.mark_called(conn, row)  # first, so an overlapping job can never call twice
        saved = prep.latest_saved(row["company"])
        source = saved.read_text() if saved else ((research.cached(conn, row["company"]) or {}).get("summary") or row["role"])
        try:
            points = await asyncio.to_thread(prep.call_points, row["company"], source)
        except Exception:
            log.exception("Call points failed for %s", row["company"])
            points = ["Review your prep guide", "Talk through your approach out loud", "Have your questions ready"]
        script = reminders.call_script(row, points)
        placed, entries = await asyncio.to_thread(actions.call, adapter, "call_reminder", summary=script)
        actions.record(conn, entries)
        if placed is True:
            await send(bot, f"Fo is calling you about your {row['company']} interview.")
        else:
            link = f"\nLink: {row['location_or_link']}" if row["location_or_link"] else ""
            await send(bot, f"I couldn't call you, so here's your interview reminder:\n\n{script}{link}")

    async def reminder_job(context):
        for row, kind in reminders.due_reminders(conn):
            await send(context.bot, reminders.format_reminder(row, kind))
            reminders.mark_reminded(conn, row, kind)
        for row in reminders.due_calls(conn):
            await place_call(context.bot, row)

    async def call_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Demo and on-demand: call now for the application's next interview, whatever the time."""
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        company, _, matches = find_by_name(conn, args_text(update))
        if len(matches) != 1:
            await reply(update, f"Usage: /call Company (no single application found for {company or '?'}).")
            return
        event = conn.execute(
            "SELECT e.*, a.company, a.role, a.next_step FROM events e JOIN applications a ON a.id = e.application_id "
            "WHERE e.application_id = ? AND e.type = 'interview' AND e.start IS NOT NULL ORDER BY e.start DESC LIMIT 1",
            (matches[0]["id"],)).fetchone()
        if event is None:
            await reply(update, f"{matches[0]['company']} has no scheduled interview to call you about.")
            return
        await reply(update, "Asking Fo to call you...")
        await place_call(context.bot, event)

    async def digest_job(context):
        await send(context.bot, reminders.daily_digest(conn, format_upcoming(conn)))

    app.job_queue.run_repeating(reminder_job, interval=15 * 60, first=20)
    app.job_queue.run_daily(digest_job, time=dt_time(8, 0, tzinfo=TZ))

    async def prep_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        company, _, matches = find_by_name(conn, args_text(update))
        if not company:
            await reply(update, "Usage: /prep Company")
        elif len(matches) != 1:
            await reply(update, f"No single application found for {company}. Try /prep Company | Role.")
        else:
            await reply(update, f"Writing the prep guide for {matches[0]['company']}...")
            await send_prep(context.bot, matches[0]["id"])

    async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        company, _, matches = find_by_name(conn, args_text(update))
        if not company:
            await reply(update, "Usage: /research Company")
            return
        name, role = (matches[0]["company"], matches[0]["role"]) if matches else (company, None)
        if research.cached(conn, name) is None:
            await reply(update, f"Researching {name}...")
        try:
            data = await get_research(name, role)
        except Exception:
            log.exception("Research failed for %s", name)
            await reply(update, f"I couldn't research {name} right now.")
            return
        await send(context.bot, research.format_research(name, data))

    # ----- Flow E: outreach, always asks first -----
    def outreach_buttons(oid):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"outreach:approve:{oid}"),
            InlineKeyboardButton("Edit", callback_data=f"outreach:edit:{oid}"),
            InlineKeyboardButton("Skip", callback_data=f"outreach:skip:{oid}"),
        ]])

    async def show_outreach(bot, oid):
        msg = await bot.send_message(chat_id=allowed_chat_id(conn), text=outreach.preview(conn, oid),
                                     reply_markup=outreach_buttons(oid), disable_web_page_preview=True)
        outreach.set_telegram_message(conn, oid, msg.message_id)

    async def suggest_outreach(bot, app_row, manual=False, given_email=None):
        """Research, pick a contact, draft, and show the draft with buttons. Returns a status line
        for /outreach, or None if a draft was shown."""
        data = await get_research(app_row["company"], app_row["role"])
        if given_email:
            contact = {"to": given_email, "source": "given by you with /outreach", "thread_id": None,
                       "reason": "You asked for an outreach draft."}
        else:
            contact = outreach.pick_contact(conn, app_row, data, manual=manual)
        if contact is None:
            return (f"I don't have a contact for {app_row['company']} (no recruiter has emailed you and the company "
                    f"doesn't publish one I found). Use /outreach {app_row['company']} name@company.com")
        email = await asyncio.to_thread(outreach.draft, outreach.build_prompt(app_row, data, contact))
        oid = outreach.save_suggestion(conn, app_row["id"], contact, email)
        await show_outreach(bot, oid)
        return None

    async def outreach_job(context):
        """Daily: suggest outreach where it would help, within the weekly limit."""
        budget = config.OUTREACH_MAX_PER_WEEK - outreach.suggested_this_week(conn)
        for app_row in outreach.candidates(conn):
            if budget <= 0:
                break
            try:
                if await suggest_outreach(context.bot, app_row) is None:
                    budget -= 1
            except Exception:
                log.exception("Outreach suggestion failed for %s", app_row["company"])

    app.job_queue.run_daily(outreach_job, time=dt_time(10, 0, tzinfo=TZ))

    async def outreach_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != allowed_chat_id(conn):
            return
        words = args_text(update).split()
        given = words.pop() if words and "@" in words[-1] else None
        company, _, matches = find_by_name(conn, " ".join(words))
        if not company:
            await reply(update, "Usage: /outreach Company [email]")
            return
        if len(matches) != 1:
            await reply(update, f"No single application found for {company}. Try /outreach Company | Role.")
            return
        app_row = matches[0]
        if "sent" in outreach.history(conn, app_row["id"]):
            await reply(update, f"You already sent an outreach email for {app_row['company']} - {app_row['role']}.")
            return
        pending = outreach.pending_for(conn, app_row["id"])
        if pending and not given:
            await show_outreach(context.bot, pending["id"])
            return
        await reply(update, f"Drafting an email for {app_row['company']}...")
        try:
            note = await suggest_outreach(context.bot, app_row, manual=True, given_email=given)
        except Exception:
            log.exception("Outreach draft failed for %s", app_row["company"])
            note = f"I couldn't draft an email for {app_row['company']} right now."
        await reply(update, note)

    async def on_outreach_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if str(query.message.chat.id) != allowed_chat_id(conn):
            await query.answer()
            return
        _, action, oid = query.data.split(":")
        oid = int(oid)
        if action == "skip":
            outreach.skip(conn, oid)
            await query.answer("Skipped")
            await query.edit_message_text(query.message.text + "\n\nSkipped. Jobby won't suggest this again.")
        elif action == "edit":
            context.chat_data["editing_outreach"] = oid
            await query.answer()
            await context.bot.send_message(chat_id=query.message.chat.id,
                                           text="What should change? Reply in your own words, e.g. \"shorter, and mention my robotics project\".")
        elif action == "approve":
            ok, msg = outreach.approve(conn, oid, query.message.chat.id)
            await query.answer(msg, show_alert=not ok)
            if not ok:
                return
            await query.edit_message_text(query.message.text + "\n\nSending...")
            try:
                my_address = await asyncio.to_thread(google_auth.my_address) if config.OUTREACH_SEND_TO_SELF else None
                request = outreach.send_request(conn, oid, my_address)
                sent_id, entries = await asyncio.to_thread(actions.call, adapter, "send_email", **request)
                actions.record(conn, entries)
            except Exception:
                log.exception("Outreach %s could not be sent", oid)
                entries = []
            if actions.succeeded(entries):
                outreach.mark_sent(conn, oid, sent_id)
                await query.edit_message_text(query.message.text.removesuffix("\n\nSending...") +
                                              f"\n\nSent to {request['to']}.")
                await sync_tracker()
            else:
                outreach.mark_failed(conn, oid)
                await query.edit_message_text(query.message.text + "\n\nSending failed. Tap Approve to try again.",
                                              reply_markup=outreach_buttons(oid))

    async def revise_outreach(update, context, oid, instructions):
        o = outreach.get(conn, oid)
        if o is None or o["status"] != "suggested":
            await reply(update, "That draft was already handled.")
            return
        app_row = db.get_application(conn, o["application_id"])
        data = await get_research(app_row["company"], app_row["role"])
        contact = {"to": o["to_address"], "source": o["contact_source"], "thread_id": o["thread_id"]}
        await reply(update, "Redrafting...")
        email = await asyncio.to_thread(outreach.draft, outreach.build_prompt(app_row, data, contact),
                                        instructions, {"subject": o["subject"], "body": o["body"]})
        outreach.update_draft(conn, oid, email)
        if o["telegram_message_id"]:
            try:
                await context.bot.edit_message_reply_markup(chat_id=allowed_chat_id(conn),
                                                            message_id=int(o["telegram_message_id"]))
            except Exception:
                pass  # old message already edited or gone
        await show_outreach(context.bot, oid)

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
    app.add_handler(CommandHandler("prep", prep_command))
    app.add_handler(CommandHandler("research", research_command))
    app.add_handler(CommandHandler("outreach", outreach_command))
    app.add_handler(CommandHandler("call", call_command))
    app.add_handler(CallbackQueryHandler(on_outreach_button, pattern=r"^outreach:"))

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
        editing = context.chat_data.pop("editing_outreach", None)
        if editing is not None:
            await revise_outreach(update, context, editing, update.effective_message.text)
            return
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
