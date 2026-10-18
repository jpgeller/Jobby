import pytest

import config
import db
import dedup
import main
import pipeline


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


@pytest.fixture
def conn(tmp_path):
    return db.init_db(tmp_path / "test.db")


def replay(conn, id_suffix=""):
    emails = sorted(main.load_fixture_emails(), key=lambda e: e["received"])
    return [pipeline.process_email(conn, dict(e, id=e["id"] + id_suffix)) for e in emails]


def snapshot(conn):
    apps = [dict(a) for a in db.list_applications(conn)]
    events = [dict(e) for e in conn.execute("SELECT * FROM events ORDER BY id")]
    for row in apps:
        row.pop("updated_at")
    return apps, events


def test_final_statuses(conn):
    replay(conn)
    by_company = {a["company"]: a for a in db.list_applications(conn)}
    assert {c: a["status"] for c, a in by_company.items()} == {
        "Northwind Robotics": "interview", "Helio Payments": "rejected", "Quanta Labs": "offer",
    }
    assert by_company["Northwind Robotics"]["source"] == "handshake"
    assert by_company["Quanta Labs"]["next_deadline"] == "2026-10-30T23:59:00-07:00"


def test_reschedule_moves_the_one_interview_event(conn):
    outcomes = replay(conn)
    events = conn.execute("SELECT * FROM events WHERE type = 'interview'").fetchall()
    assert len(events) == 1
    assert events[0]["start"] == "2026-10-26T11:00:00-07:00"
    assert outcomes[-1]["action"] == "updated"


def test_replay_twice_is_a_no_op(conn):
    replay(conn)
    before = snapshot(conn)
    assert all(o["action"] == "already_processed" for o in replay(conn))
    assert snapshot(conn) == before


def test_same_emails_with_new_ids_create_no_duplicates(conn):
    # e.g. a forwarded copy: different message id, same content.
    replay(conn)
    before = snapshot(conn)
    replay(conn, id_suffix="-copy")
    assert snapshot(conn) == before


def test_late_older_email_does_not_move_status_back(conn):
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    pipeline.process_email(conn, emails["fixture-03"])  # interview first
    outcome = pipeline.process_email(conn, emails["fixture-02"])  # older assessment arrives late
    app = db.get_application(conn, outcome["application_id"])
    assert app["status"] == "interview"
    assert app["next_step"] == "Technical interview with engineering team"


@pytest.mark.parametrize("current, proposed, expected", [
    ("applied", "interview", "interview"),
    ("interview", "assessment", "interview"),
    ("interview", "rejected", "rejected"),
    ("rejected", "applied", "rejected"),
    ("assessment", "offer", "offer"),
    ("withdrawn", "interview", "withdrawn"),
])
def test_next_status(current, proposed, expected):
    assert dedup.next_status(current, proposed) == expected


def test_company_matching_ignores_suffixes_and_case(conn):
    db.insert_application(conn, company="Northwind Robotics, Inc.", company_norm=dedup.normalize_company("Northwind Robotics, Inc."),
                          role="Software Engineering Intern, Summer 2027")
    assert dedup.find_application(conn, "northwind robotics", "Software Engineering Intern")
    assert dedup.find_application(conn, "Southwind Robotics", "Software Engineering Intern") is None


def test_two_roles_at_one_company_stay_separate(conn):
    for role in ["Software Engineering Intern", "Data Science Intern"]:
        db.insert_application(conn, company="Acme", company_norm="acme", role=role)
    assert dedup.find_application(conn, "Acme", "Data Science Intern")["role"] == "Data Science Intern"
    assert dedup.find_application(conn, "Acme", "Product Design Intern") is None
    assert dedup.find_application(conn, "Acme", None) is None


def test_deadlines_become_events(conn):
    replay(conn)
    rows = conn.execute("SELECT title, start, \"end\" FROM events WHERE type = 'deadline' ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Deadline: Complete HackerRank OA - Northwind Robotics", "2026-10-19T23:29:00-07:00", "2026-10-19T23:59:00-07:00"),
        ("Deadline: Sign and return offer letter - Quanta Labs", "2026-10-30T23:29:00-07:00", "2026-10-30T23:59:00-07:00"),
    ]


def test_no_role_matches_single_application_or_asks(conn, monkeypatch):
    import classifier
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    pipeline.process_email(conn, emails["fixture-03"])  # Northwind interview
    resched = dict(classifier.classify_email(emails["fixture-04"]), role=None)
    assert pipeline.process_email(conn, emails["fixture-04"], resched)["action"] == "updated"
    unknown = dict(resched, company="Brand New Co")  # no role, but the company is new: track it
    assert pipeline.process_email(conn, dict(emails["fixture-04"], id="x"), unknown)["action"] == "created"


def test_confirmation_without_role_creates_then_fills_role(conn):
    import classifier
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    conf = dict(classifier.classify_email(emails["fixture-01"]), role=None)
    out = pipeline.process_email(conn, emails["fixture-01"], conf)
    assert out["action"] == "created" and db.get_application(conn, out["application_id"])["role"] == ""
    out2 = pipeline.process_email(conn, emails["fixture-02"])  # OA email names the role
    assert out2["application_id"] == out["application_id"]
    app = db.get_application(conn, out["application_id"])
    assert app["role"] == "Software Engineering Intern, Summer 2027" and app["status"] == "assessment"
    assert len(db.list_applications(conn)) == 1


def test_no_role_with_several_applications_asks(conn):
    import classifier
    emails = {e["id"]: e for e in main.load_fixture_emails()}
    for role in ["SWE Intern", "Data Intern"]:
        db.insert_application(conn, company="Northwind Robotics", company_norm="northwind robotics", role=role)
    conf = dict(classifier.classify_email(emails["fixture-01"]), role=None)
    assert pipeline.process_email(conn, emails["fixture-01"], conf)["action"] == "needs_review"
