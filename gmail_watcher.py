"""Fetch new application-related emails from Gmail, read-only (spec Section 5).

The functions here only talk to Gmail; they never touch the database, so they can run in a
worker thread."""
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import config

QUERY = ('newer_than:30d (from:handshake OR from:joinhandshake.com OR from:interstride '
         'OR subject:(application OR interview OR assessment OR "coding challenge" '
         'OR "next steps" OR offer OR "thank you for applying" OR unfortunately)) '
         # Skip mail Jobby sent: real outreach only sits in Sent, and test-mode outreach copies are
         # tagged. Emails you send yourself still count, since they also land in the inbox.
         '(in:inbox OR -in:sent) -subject:"jobby outreach test"')

# In mock mode the classifier can't really read emails, so only emails whose subject carries
# this marker are processed. Everything else waits, unprocessed, until live mode.
TEST_MARKER = "[jobby test]"


def current_query():
    if config.LLM_MODE == "mock":
        return f'{QUERY} subject:"jobby test"'
    return QUERY


def gmail_service():
    import google_auth
    return google_auth.service("gmail", "v1")


def list_message_ids(service, query=QUERY, limit=100):
    ids, page = [], None
    while len(ids) < limit:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page,
                                               maxResults=min(100, limit - len(ids))).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def get_email(service, message_id):
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return parse_message(msg)


def parse_message(msg):
    """Gmail API message -> the email dict the pipeline uses."""
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, ZoneInfo(config.TIMEZONE))
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "received": received.isoformat(),
        "body": _body(msg["payload"]) or msg.get("snippet", ""),
    }


def is_test_email(email):
    return TEST_MARKER in email["subject"].lower()


def _body(payload):
    """Prefer the text/plain part; fall back to text/html (the classifier strips the tags)."""
    plain = _find_part(payload, "text/plain")
    return plain if plain is not None else (_find_part(payload, "text/html") or "")


def _find_part(part, mime):
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        found = _find_part(sub, mime)
        if found is not None:
            return found
    return None
