import pytest

import config
import db
import main
import pipeline
import trackers
from trackers.sheets import HEADERS, SheetsTracker


class FakeSheet(SheetsTracker):
    """SheetsTracker with the API calls replaced by an in-memory grid."""

    def __init__(self):
        super().__init__("fake-id")
        self.grid, self.writes = [], 0

    def _read(self):
        return [list(r) for r in self.grid]

    def _write(self, updates, appends):
        self.writes += len(updates) + len(appends)
        for row, values in updates:
            while len(self.grid) < row:
                self.grid.append([])
            self.grid[row - 1] = list(values)
        self.grid.extend(list(v) for v in appends)


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODE", "mock")


@pytest.fixture
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    for email in sorted(main.load_fixture_emails(), key=lambda e: e["received"]):
        pipeline.process_email(conn, email)
    return conn


def test_first_sync_writes_header_and_all_rows(conn):
    sheet = FakeSheet()
    assert trackers.sync_tracker(conn, sheet) == 3
    assert sheet.grid[0] == HEADERS
    assert [r[1] for r in sheet.grid[1:]] == ["Northwind Robotics", "Helio Payments", "Quanta Labs"]
    assert sheet.grid[3][5] == "2026-10-30 23:59"  # deadline shown in Pacific time
    assert [a["tracker_row_id"] for a in db.list_applications(conn)] == ["1", "2", "3"]


def test_unchanged_sync_writes_nothing(conn):
    sheet = FakeSheet()
    trackers.sync_tracker(conn, sheet)
    sheet.writes = 0
    trackers.sync_tracker(conn, sheet)
    assert sheet.writes == 0


def test_status_change_updates_only_that_row_even_after_sorting(conn):
    sheet = FakeSheet()
    trackers.sync_tracker(conn, sheet)
    sheet.grid[1:] = sorted(sheet.grid[1:], key=lambda r: r[1])  # user sorts by company
    db.update_application(conn, 2, status="withdrawn")
    conn.commit()
    sheet.writes = 0
    trackers.sync_tracker(conn, sheet)
    assert sheet.writes == 1
    helio = next(r for r in sheet.grid if r[1] == "Helio Payments")
    assert helio[3] == "withdrawn"
    assert len(sheet.grid) == 4


def test_new_application_is_appended(conn):
    sheet = FakeSheet()
    trackers.sync_tracker(conn, sheet)
    db.insert_application(conn, company="Atlas Maps", company_norm="atlas maps", role="Software Intern")
    conn.commit()
    trackers.sync_tracker(conn, sheet)
    assert sheet.grid[-1][:4] == ["4", "Atlas Maps", "Software Intern", "applied"]
