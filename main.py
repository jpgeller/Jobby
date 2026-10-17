"""Jobby entry point. Later milestones add the scheduler and the Telegram bot here."""
import argparse

import config
import db


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
        print("--replay-fixtures arrives in milestone 2.")


if __name__ == "__main__":
    main()
