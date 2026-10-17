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


def init_db(path=None):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(r["name"] for r in rows)
