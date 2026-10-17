"""Pick the tracker backend and sync the database into it."""
import config
import db


def get_tracker(conn):
    """The configured tracker, creating a Google Sheet the first time if no ID is set."""
    if config.TRACKER_BACKEND != "sheets":
        raise ValueError(f"Tracker backend {config.TRACKER_BACKEND!r} is not built yet")
    from trackers.sheets import SheetsTracker

    sheet_id = config.GOOGLE_SHEET_ID or db.get_setting(conn, "google_sheet_id")
    if sheet_id:
        return SheetsTracker(sheet_id)
    tracker, url = SheetsTracker.create()
    db.set_setting(conn, "google_sheet_id", tracker.spreadsheet_id)
    print(f"Created tracker sheet: {url}")
    return tracker


def tracker_url(conn):
    sheet_id = config.GOOGLE_SHEET_ID or db.get_setting(conn, "google_sheet_id")
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else None


def rows_to_sync(conn):
    return [dict(a) for a in db.list_applications(conn)]


def save_row_ids(conn, row_ids):
    for app_id, row_id in row_ids.items():
        conn.execute("UPDATE applications SET tracker_row_id = ? WHERE id = ? AND tracker_row_id IS NOT ?",
                     (row_id, app_id, row_id))
    conn.commit()


def sync_tracker(conn, tracker):
    """Mirror every application. Split into three steps so the network call can run in a
    worker thread while the SQLite reads and writes stay on the main thread."""
    row_ids = tracker.sync(rows_to_sync(conn))
    save_row_ids(conn, row_ids)
    return len(row_ids)
