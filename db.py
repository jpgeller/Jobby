"""SQLite schema and connection. The database is the single source of truth (spec Section 2)."""
import sqlite3

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company        TEXT NOT NULL,
    company_norm   TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'other'
                   CHECK (source IN ('handshake', 'interstride', 'direct', 'other')),
    status         TEXT NOT NULL DEFAULT 'applied'
                   CHECK (status IN ('applied', 'assessment', 'interview', 'offer', 'rejected', 'withdrawn')),
    applied_date   TEXT,
    next_step      TEXT,
    next_deadline  TEXT,
    job_url        TEXT,
    notes          TEXT,
    tracker_row_id TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_applications_company_norm ON applications(company_norm);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id    INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    type              TEXT NOT NULL DEFAULT 'other'
                      CHECK (type IN ('interview', 'assessment', 'deadline', 'other')),
    title             TEXT NOT NULL,
    start             TEXT,
    "end"             TEXT,
    location_or_link  TEXT,
    calendar_event_id TEXT,
    email_received_at TEXT,  -- received time of the email that last set this event
    calendar_dirty    INTEGER NOT NULL DEFAULT 1,  -- 1 until the calendar has this version
    reminded_24h      INTEGER NOT NULL DEFAULT 0,
    reminded_2h       INTEGER NOT NULL DEFAULT 0,
    called            INTEGER NOT NULL DEFAULT 0,
    prep_sent         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outreach (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id      INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    to_address          TEXT,
    contact_source      TEXT,
    subject             TEXT,
    body                TEXT,
    status              TEXT NOT NULL DEFAULT 'suggested'
                        CHECK (status IN ('suggested', 'approved', 'sent', 'skipped')),
    telegram_message_id TEXT,
    gmail_sent_id       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at             TEXT
);

CREATE TABLE IF NOT EXISTS actions_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL DEFAULT (datetime('now')),
    backend TEXT NOT NULL CHECK (backend IN ('fo', 'google')),
    action  TEXT NOT NULL,
    request TEXT,
    result  TEXT,
    success INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS company_research (
    company_norm  TEXT PRIMARY KEY,
    summary       TEXT,
    recent_news   TEXT,
    sources       TEXT,
    researched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS processed_emails (
    gmail_message_id TEXT PRIMARY KEY,
    processed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    classification   TEXT,
    application_id   INTEGER REFERENCES applications(id) ON DELETE SET NULL
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS won't add them to an
# existing database, so add them here instead of deleting jobby.db.
MIGRATIONS = [
    ("events", "calendar_dirty", "INTEGER NOT NULL DEFAULT 1"),
    ("processed_emails", "from_address", "TEXT"),
    ("processed_emails", "thread_id", "TEXT"),
    ("outreach", "thread_id", "TEXT"),
    ("outreach", "reason", "TEXT"),
    ("company_research", "contact_email", "TEXT"),
    ("company_research", "contact_url", "TEXT"),
]


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(r["name"] for r in rows)


# ---------- applications ----------

def get_application(conn, app_id):
    return conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()


def list_applications(conn):
    return conn.execute("SELECT * FROM applications ORDER BY id").fetchall()


def insert_application(conn, **fields):
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO applications ({cols}) VALUES ({marks})", tuple(fields.values()))
    return cur.lastrowid


def update_application(conn, app_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE applications SET {sets}, updated_at = datetime('now') WHERE id = ?",
                 (*fields.values(), app_id))


# ---------- events ----------

def insert_event(conn, **fields):
    cols = ", ".join(f'"{k}"' for k in fields)
    marks = ", ".join("?" for _ in fields)
    return conn.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid


def update_event(conn, event_id, **fields):
    sets = ", ".join(f'"{k}" = ?' for k in fields)
    conn.execute(f"UPDATE events SET {sets} WHERE id = ?", (*fields.values(), event_id))


def dirty_events(conn):
    """Events whose latest version is not on the calendar yet, with their application."""
    return conn.execute(
        "SELECT e.*, a.company, a.role, a.next_step FROM events e JOIN applications a ON a.id = e.application_id "
        "WHERE e.calendar_dirty = 1 AND e.start IS NOT NULL ORDER BY e.id").fetchall()


def log_action(conn, backend, action, request, result, success):
    conn.execute("INSERT INTO actions_log (backend, action, request, result, success) VALUES (?, ?, ?, ?, ?)",
                 (backend, action, request, result, int(success)))
    conn.commit()


def latest_event(conn, app_id, event_type):
    return conn.execute("SELECT * FROM events WHERE application_id = ? AND type = ? ORDER BY id DESC LIMIT 1",
                        (app_id, event_type)).fetchone()


# ---------- settings ----------

def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


# ---------- processed emails ----------

def is_email_processed(conn, message_id):
    return conn.execute("SELECT 1 FROM processed_emails WHERE gmail_message_id = ?", (message_id,)).fetchone() is not None


def mark_email_processed(conn, message_id, classification, app_id=None, from_address=None, thread_id=None):
    conn.execute("INSERT INTO processed_emails (gmail_message_id, classification, application_id, from_address, "
                 "thread_id) VALUES (?, ?, ?, ?, ?)", (message_id, classification, app_id, from_address, thread_id))
