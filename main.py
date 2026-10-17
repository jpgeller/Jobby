"""Jobby entry point: starts the Telegram bot, which also runs the inbox and tracker jobs."""
import argparse
import json

import config
import db
import pipeline
import telegram_bot
import trackers

FIXTURE_EMAILS = config.ROOT / "fixtures" / "emails"


def load_fixture_emails():
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_EMAILS.glob("*.json"))]


# The demo starts here: Northwind at the assessment stage, so a test interview email visibly moves it.
DEMO_SEED = ["fixture-01", "fixture-02", "fixture-05", "fixture-06"]
DATA_TABLES = ["events", "outreach", "actions_log", "processed_emails", "company_research", "applications"]


def demo_reset(conn):
    """Wipe applications and events (keeping settings like the chat and sheet IDs), seed the demo
    starting point, and clear the tracker sheet so it mirrors the fresh database."""
    for table in DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    emails = {e["id"]: e for e in load_fixture_emails()}
    for email_id in DEMO_SEED:
        pipeline.process_email(conn, emails[email_id])
    tracker = trackers.get_tracker(conn)
    tracker.clear()
    trackers.sync_tracker(conn, tracker)
    print(f"Demo reset: {len(db.list_applications(conn))} applications seeded, tracker cleared and resynced.")


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
    parser = argparse.ArgumentParser(description="Jobby: internship application agent")
    parser.add_argument("--replay-fixtures", action="store_true",
                        help="feed fixtures/emails through the pipeline without Gmail")
    parser.add_argument("--demo-reset", action="store_true",
                        help="wipe applications and seed the demo starting point (keeps settings)")
    args = parser.parse_args()

    conn = db.init_db()
    print(f"Jobby started. LLM mode: {config.LLM_MODE}. Actions: {config.ACTIONS_BACKEND}. "
          f"Tracker: {config.TRACKER_BACKEND}.")
    print(f"Database ready at {config.DB_PATH.name}: {', '.join(db.table_names(conn))}")

    if args.demo_reset:
        demo_reset(conn)
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
    telegram_bot.run(conn, tracker)


if __name__ == "__main__":
    main()
