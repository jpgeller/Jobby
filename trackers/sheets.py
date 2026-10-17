"""Google Sheets tracker: one row per application, keyed by the Jobby ID in column A,
so rows stay matched even if you sort or filter the sheet."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from trackers.base import TrackerAdapter

TAB = "Applications"
HEADERS = ["ID", "Company", "Role", "Status", "Next step", "Deadline", "Applied", "Source", "Job URL", "Notes",
           "Updated"]
LAST_COL = chr(ord("A") + len(HEADERS) - 1)
TZ = ZoneInfo(config.TIMEZONE)


def to_row(app):
    """All cells as strings, so they compare equal to what the API reads back."""
    return [
        str(app["id"]),
        app["company"] or "",
        app["role"] or "",
        app["status"] or "",
        app["next_step"] or "",
        _fmt_iso(app["next_deadline"]),
        app["applied_date"] or "",
        app["source"] or "",
        app["job_url"] or "",
        app["notes"] or "",
        _fmt_utc(app["updated_at"]),
    ]


def _fmt_iso(value):
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%Y-%m-%d %H:%M") if value else ""


def _fmt_utc(value):
    """SQLite's datetime('now') is UTC without an offset."""
    if not value:
        return ""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


class SheetsTracker(TrackerAdapter):
    def __init__(self, spreadsheet_id, service=None):
        self.spreadsheet_id = spreadsheet_id
        self._service = service

    @classmethod
    def create(cls, title="Jobby tracker", service=None):
        """Make a new spreadsheet with the Applications tab. Returns (tracker, url)."""
        tracker = cls(None, service)
        sheet = tracker.api.create(body={
            "properties": {"title": title},
            "sheets": [{"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}],
        }, fields="spreadsheetId,spreadsheetUrl").execute()
        tracker.spreadsheet_id = sheet["spreadsheetId"]
        return tracker, sheet["spreadsheetUrl"]

    @property
    def api(self):
        if self._service is None:
            import google_auth
            self._service = google_auth.service("sheets", "v4")
        return self._service.spreadsheets()

    # ----- TrackerAdapter -----

    def sync(self, apps):
        rows = self._read()
        updates, appends = [], []
        if not rows or rows[0] != HEADERS:
            updates.append((1, HEADERS))
        row_of_id = {r[0]: i for i, r in enumerate(rows, start=1) if i > 1 and r and r[0]}
        for app in apps:
            values = to_row(app)
            row = row_of_id.get(values[0])
            if row is None:
                appends.append(values)
            elif _pad(rows[row - 1]) != values:
                updates.append((row, values))
        self._write(updates, appends)
        return {app["id"]: str(app["id"]) for app in apps}

    def upsert_application(self, app):
        return self.sync([app])[app["id"]]

    def list_rows(self):
        rows = self._read()
        return [dict(zip(HEADERS, _pad(r))) for r in rows[1:] if r and r[0]]

    def delete_application(self, tracker_row_id):
        rows = self._read()
        for i, r in enumerate(rows):
            if i > 0 and r and r[0] == tracker_row_id:
                self.api.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [{
                    "deleteDimension": {"range": {"sheetId": self._tab_id(), "dimension": "ROWS",
                                                  "startIndex": i, "endIndex": i + 1}}}]}).execute()
                return

    # ----- Sheets API calls (overridden in tests) -----

    def _read(self):
        self._ensure_tab()
        result = self.api.values().get(spreadsheetId=self.spreadsheet_id, range=f"{TAB}!A:{LAST_COL}").execute()
        return result.get("values", [])

    def _write(self, updates, appends):
        if updates:
            self.api.values().batchUpdate(spreadsheetId=self.spreadsheet_id, body={
                "valueInputOption": "RAW",
                "data": [{"range": f"{TAB}!A{row}:{LAST_COL}{row}", "values": [values]} for row, values in updates],
            }).execute()
        if appends:
            self.api.values().append(spreadsheetId=self.spreadsheet_id, range=f"{TAB}!A1",
                                     valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                                     body={"values": appends}).execute()

    def _tab_id(self):
        meta = self.api.get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties").execute()
        return next(s["properties"]["sheetId"] for s in meta["sheets"] if s["properties"]["title"] == TAB)

    def _ensure_tab(self):
        """A sheet you made yourself may not have the Applications tab yet."""
        if getattr(self, "_tab_ok", False):
            return
        meta = self.api.get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title").execute()
        if TAB not in [s["properties"]["title"] for s in meta["sheets"]]:
            self.api.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [{
                "addSheet": {"properties": {"title": TAB, "gridProperties": {"frozenRowCount": 1}}}}]}).execute()
        self._tab_ok = True


def _pad(row):
    return list(row) + [""] * (len(HEADERS) - len(row))
