"""Jobby entry point: starts the Telegram bot, which also runs the inbox and tracker jobs."""
import argparse
import logging
import json

import actions
import calendar_sync
import config
import db
import gmail_watcher
import pipeline
import telegram_bot
import trackers

FIXTURE_EMAILS = config.ROOT / "fixtures" / "emails"


def load_fixture_emails():
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_EMAILS.glob("*.json"))]


# The demo starts here: Northwind at the assessment stage, so a test interview email visibly moves it.
DEMO_SEED = ["fixture-01", "fixture-02", "fixture-05", "fixture-06"]
DATA_TABLES = ["events", "outreach", "actions_log", "processed_emails", "company_research", "applications"]


def wipe(conn, seed=()):
    """Delete applications, events, and history (keeping settings like the chat and sheet IDs),
    remove the calendar events Jobby created, optionally seed fixture emails, and clear the sheet so
    it mirrors the database. Jobby's own test emails are marked handled so they never count."""
    # Setup, not an action Jobby takes for you: use Google directly so it is fast and predictable.
    adapter = actions.get_actions(backend="google")
    removed = 0
    for row in conn.execute("SELECT calendar_event_id FROM events WHERE calendar_event_id IS NOT NULL").fetchall():
        _, entries = actions.call(adapter, "delete_event", event_id=row["calendar_event_id"])
        removed += actions.succeeded(entries)
    for table in DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    emails = {e["id"]: e for e in load_fixture_emails()}
    for email_id in seed:
        pipeline.process_email(conn, emails[email_id])
    # [Jobby test] / [Jobby outreach test] emails count as handled: in a demo the invite only
    # "arrives" when you send a fresh one, and in live mode they are never read as real invites.
    gmail = gmail_watcher.gmail_service()
    tests = gmail_watcher.list_message_ids(gmail, 'subject:"jobby test" OR subject:"jobby outreach test"', limit=500)
    for message_id in tests:
        db.mark_email_processed(conn, message_id, "jobby_test_skip")
    conn.commit()
    tracker = trackers.get_tracker(conn)
    tracker.clear()
    trackers.sync_tracker(conn, tracker)
    added = calendar_sync.sync_calendar(conn, adapter)
    print(f"Wiped. {len(db.list_applications(conn))} applications seeded, {len(tests)} Jobby test emails skipped, "
          f"{removed} old calendar events removed, {added} added.")


def replay_fixtures(conn):
    # Oldest first, like a real inbox catching up, so statuses move in order.
    for email in sorted(load_fixture_emails(), key=lambda e: e["received"]):
        outcome = pipeline.process_email(conn, email)
        category = outcome.get("classification", {}).get("category", "-")
        status = ""
        if outcome.get("new_status"):
            status = f"{outcome['old_status'] or 'new'} -> {outcome['new_status']}"
        print(f"{email['id']}  {category:<25} {outcome['action']:<18} {status}")

    print("\nApplications:")
    for a in db.list_applications(conn):
        print(f"  #{a['id']} {a['company']} | {a['role']} | {a['status']} | next: {a['next_step']} "
              f"| deadline: {a['next_deadline']}")
    print("Events:")
    for e in conn.execute("SELECT * FROM events ORDER BY id"):
        print(f"  #{e['id']} {e['title']} | {e['start']} -> {e['end']}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP libraries log full request URLs at INFO, and Telegram's include the bot token.
    for noisy in ("httpx", "httpx2", "telegram", "apscheduler", "mcp", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Jobby: internship application agent")
    parser.add_argument("--replay-fixtures", action="store_true",
                        help="feed fixtures/emails through the pipeline without Gmail")
    parser.add_argument("--demo-reset", action="store_true",
                        help="mock mode only: wipe and seed the demo starting point (keeps settings)")
    parser.add_argument("--wipe", action="store_true",
                        help="delete all applications and history to start over (asks first; keeps settings)")
    args = parser.parse_args()

    conn = db.init_db()
    print(f"Jobby started. LLM mode: {config.LLM_MODE}. Actions: {config.ACTIONS_BACKEND}. "
          f"Tracker: {config.TRACKER_BACKEND}.")
    print(f"Database ready at {config.DB_PATH.name}: {', '.join(db.table_names(conn))}")

    if config.LLM_MODE == "live" and (args.demo_reset or args.replay_fixtures):
        print("Refusing in live mode: demo data would mix with your real applications. "
              "Use LLM_MODE=mock with a separate DB_PATH for demos.")
        return

    if args.demo_reset:
        wipe(conn, seed=DEMO_SEED)
        return

    if args.wipe:
        answer = input("This deletes every application, event, and outreach record (settings and logins stay). "
                       "Type WIPE to continue: ")
        if answer.strip() == "WIPE":
            wipe(conn)
        else:
            print("Nothing deleted.")
        return

    if args.replay_fixtures:
        replay_fixtures(conn)
        return

    tracker = None
    try:
        tracker = trackers.get_tracker(conn)
        print(f"Tracker: {trackers.tracker_url(conn)} ({trackers.sync_tracker(conn, tracker)} rows synced)")
    except Exception as e:
        print(f"Tracker unavailable, continuing without it: {e}")
    adapter = actions.get_actions()
    try:
        print(f"Calendar: {calendar_sync.sync_calendar(conn, adapter)} events synced")
    except Exception as e:
        print(f"Calendar sync failed, will retry while running: {e}")
    telegram_bot.run(conn, tracker, adapter)


if __name__ == "__main__":
    main()
