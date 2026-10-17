import db


def test_init_db_creates_all_tables(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    assert db.table_names(conn) == [
        "actions_log", "applications", "company_research", "events", "outreach", "processed_emails", "settings",
    ]


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path).execute("INSERT INTO applications (company, company_norm) VALUES ('Stripe', 'stripe')").connection.commit()
    conn = db.init_db(path)
    assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
