"""Jobby entry point. Later milestones add the scheduler and the Telegram bot here."""
import argparse
import json

import classifier
import config
import db

FIXTURE_EMAILS = config.ROOT / "fixtures" / "emails"


def load_fixture_emails():
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_EMAILS.glob("*.json"))]


def replay_fixtures():
    for email in load_fixture_emails():
        result = classifier.classify_email(email)
        print(f"\n== {email['id']}: {email['subject']}")
        print(json.dumps(result, indent=2, ensure_ascii=False))


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
        replay_fixtures()


if __name__ == "__main__":
    main()
