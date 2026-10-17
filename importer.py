"""Flow A (spec Section 6): import past applications from Handshake screenshots."""
import re
from datetime import date

import config
import db
import dedup
import llm

STATUSES = ["applied", "assessment", "interview", "offer", "rejected", "withdrawn"]

SCHEMA = {
    "type": "object",
    "properties": {
        "applications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "applied_date": {"type": ["string", "null"]},
                    "status": {"type": "string", "enum": STATUSES},
                },
                "required": ["company", "role", "applied_date", "status"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["applications"],
    "additionalProperties": False,
}

SYSTEM = """You read screenshots of a student's internship applications page (Handshake or Interstride) and list every application visible.

- company: the employer's name as shown. role: the full job title as shown.
- applied_date: YYYY-MM-DD, or null if not shown. If the year is missing, pick the most recent date on or before today.
- status, mapped from the platform's label:
  applied: Applied, Submitted, Pending, Under review, Reviewed
  assessment: Assessment, Online assessment, Coding challenge
  interview: Interviewing, Interview scheduled
  offer: Offer, Offered, Hired
  rejected: Not selected, Declined by employer, Rejected, Position filled, Closed
  withdrawn: Withdrawn
  If unsure, use applied.
- Only list rows you can read clearly. Never invent applications."""


def parse_screenshot(image_bytes, media_type, today=None):
    prompt = f"Today is {(today or date.today()).isoformat()}. List the applications in this screenshot."
    result = llm.ask_claude_vision([(image_bytes, media_type)], prompt, task="parse_screenshot",
                                   system=SYSTEM, model=config.MODEL_SMART, schema=SCHEMA)
    return llm.parse_json(result, "parse_screenshot")["applications"]


class ImportSession:
    """Applications found so far in one /import, waiting for the user's ok."""

    def __init__(self):
        self.items = []

    def add(self, found):
        """Add parsed rows, skipping repeats from overlapping screenshots. Returns how many were new."""
        added = 0
        for item in found:
            if not item.get("company") or not item.get("role"):
                continue
            if any(self._same(item, existing) for existing in self.items):
                continue
            self.items.append(item)
            added += 1
        return added

    def remove(self, numbers):
        """Remove 1-based item numbers. Returns the numbers that were out of range."""
        bad = [n for n in numbers if not 1 <= n <= len(self.items)]
        for n in sorted(set(numbers) - set(bad), reverse=True):
            del self.items[n - 1]
        return bad

    def preview(self, conn):
        if not self.items:
            return "No applications found yet. Send a screenshot of your Handshake Applications page."
        lines = [f"Found {len(self.items)}:"]
        for i, item in enumerate(self.items, 1):
            tracked = " (already tracked, will skip)" if dedup.find_application(conn, item["company"], item["role"]) else ""
            when = f", {item['applied_date']}" if item.get("applied_date") else ""
            lines.append(f"{i}. {item['company']} - {item['role']} ({item['status']}{when}){tracked}")
        lines.append('\nSend more screenshots, or reply "ok" to import, "remove 3" to drop one, or "cancel".')
        return "\n".join(lines)

    def commit(self, conn, source="handshake"):
        """Insert the items. Returns (imported, skipped_as_already_tracked)."""
        imported = skipped = 0
        for item in self.items:
            if dedup.find_application(conn, item["company"], item["role"]):
                skipped += 1
                continue
            db.insert_application(conn, company=item["company"], company_norm=dedup.normalize_company(item["company"]),
                                  role=item["role"], source=source, status=item["status"],
                                  applied_date=item.get("applied_date"))
            imported += 1
        conn.commit()
        self.items = []
        return imported, skipped

    @staticmethod
    def _same(a, b):
        return (dedup.normalize_company(a["company"]) == dedup.normalize_company(b["company"])
                and a["role"].strip().lower() == b["role"].strip().lower())


def handle_reply(conn, session, text):
    """Handle a text reply during an import. Returns (reply, done)."""
    cmd = text.strip().lower()
    if cmd in {"ok", "okay", "yes", "import"}:
        if not session.items:
            return "Nothing to import yet. Send a screenshot first, or reply \"cancel\".", False
        imported, skipped = session.commit(conn)
        note = f" Skipped {skipped} already tracked." if skipped else ""
        return f"Imported {imported} applications.{note}", True
    if cmd in {"cancel", "stop"}:
        return "Import cancelled. Nothing was added.", True
    if cmd.startswith("remove"):
        numbers = [int(n) for n in re.findall(r"\d+", cmd)]
        if not numbers:
            return 'Say which one, e.g. "remove 3" or "remove 2 5".', False
        bad = session.remove(numbers)
        warn = f"No item {', '.join(map(str, bad))}.\n\n" if bad else ""
        return warn + session.preview(conn), False
    return 'During an import, reply "ok", "remove 3", or "cancel", or send another screenshot.', False
