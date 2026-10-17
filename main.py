"""Jobby entry point. Later milestones add the scheduler and the Telegram bot here."""
import argparse
import json

import config
import db
import pipeline

FIXTURE_EMAILS = config.ROOT / "fixtures" / "emails"


def load_fixture_emails():
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_EMAILS.glob("*.json"))]


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
                        help="feed fixtures/emails through the pipeline without Gmail (milestone 2)")
    args = parser.parse_args()

    conn = db.init_db()
    print(f"Jobby started. LLM mode: {config.LLM_MODE}. Actions: {config.ACTIONS_BACKEND}. "
          f"Tracker: {config.TRACKER_BACKEND}.")
    print(f"Database ready at {config.DB_PATH.name}: {', '.join(db.table_names(conn))}")

    if args.replay_fixtures:
        replay_fixtures(conn)


if __name__ == "__main__":
    main()
