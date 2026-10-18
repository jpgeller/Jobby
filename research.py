"""Company research with web search, cached per company for 7 days (spec Section 3).

fetch() only calls Claude, so it can run in a worker thread; cached() and save() use the
database and stay on the main thread."""
import json
import re
from datetime import datetime, timedelta, timezone

import config
import dedup
import llm

CACHE_DAYS = 7
CONTACT_CACHE_DAYS = 30
CONTACT_SEARCHES = 3

CONTACT_SYSTEM = """Find a recruiting or university-recruiting email address that the company publishes on its own
website or in a job post. Answer in exactly this format:

RECRUITING CONTACT:
<the email, then the URL where it appears, e.g. "campus@acme.com https://acme.com/careers", or "none">

Never guess an address, and never give a personal email."""

SYSTEM = """You research companies for a student preparing internship applications and interviews.
Search the web, then answer in exactly this format, plain text, no markdown headings:

SUMMARY:
<4-6 sentences: what the company does, main products and customers, and anything about the team or product area related to the role>

RECENT NEWS:
- <one line per item, newest first, at most 4, each from the last 12 months; write "- None found" if there is nothing>

RECRUITING CONTACT:
<a recruiting or university-recruiting email address published on the company's own website or a job post, then the URL where it appears, e.g. "campus@acme.com https://acme.com/careers". Write "none" if you did not find one. Never guess an address, and never give a personal email.>

Only state what your sources support. Never invent news or contacts."""


def cached(conn, company):
    """The cached research row if it is fresh, else None."""
    row = conn.execute("SELECT * FROM company_research WHERE company_norm = ?",
                       (dedup.normalize_company(company),)).fetchone()
    if not row:
        return None
    researched = datetime.fromisoformat(row["researched_at"]).replace(tzinfo=timezone.utc)
    if row["summary"] is None or datetime.now(timezone.utc) - researched > timedelta(days=CACHE_DAYS):
        return None  # summary is NULL when only a contact lookup was saved
    return {"summary": row["summary"], "recent_news": row["recent_news"], "sources": json.loads(row["sources"] or "[]"),
            "contact_email": row["contact_email"], "contact_url": row["contact_url"]}


def cached_contact(conn, company):
    """{contact_email, contact_url} from a lookup in the last 30 days (email may be None: none found),
    or None if the company hasn't been checked recently."""
    row = conn.execute("SELECT contact_email, contact_url, contact_checked_at FROM company_research "
                       "WHERE company_norm = ?", (dedup.normalize_company(company),)).fetchone()
    if not row or not row["contact_checked_at"]:
        return None
    checked = datetime.fromisoformat(row["contact_checked_at"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - checked > timedelta(days=CONTACT_CACHE_DAYS):
        return None
    return {"contact_email": row["contact_email"], "contact_url": row["contact_url"]}


def fetch_contact(company):
    """A cheap search (3 searches max) just for a published recruiting address. Worker-thread safe."""
    text, _ = llm.ask_claude_research(f"Company: {company}\n\nFind their published recruiting email.",
                                      task="find_contact", system=CONTACT_SYSTEM, model=config.MODEL_SMART,
                                      max_searches=CONTACT_SEARCHES, max_tokens=2000)
    return _parse_contact(text.partition("RECRUITING CONTACT:")[2])


def save_contact(conn, company, data):
    conn.execute(
        "INSERT INTO company_research (company_norm, contact_email, contact_url, contact_checked_at) "
        "VALUES (?, ?, ?, datetime('now')) ON CONFLICT(company_norm) DO UPDATE SET "
        "contact_email = excluded.contact_email, contact_url = excluded.contact_url, "
        "contact_checked_at = excluded.contact_checked_at",
        (dedup.normalize_company(company), data["contact_email"], data["contact_url"]))
    conn.commit()


def _parse_contact(text):
    email = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    url = re.search(r"https?://\S+", text)
    # A contact only counts with the page it was published on.
    return {"contact_email": email.group(0) if email and url else None,
            "contact_url": url.group(0) if email and url else None}


def fetch(company, role=None):
    prompt = f"Company: {company}\nRole: {role or 'internship'}\n\nResearch this company for my interview prep."
    text, sources = llm.ask_claude_research(prompt, task="research_company", system=SYSTEM, model=config.MODEL_SMART)
    summary, _, rest = text.partition("RECENT NEWS:")
    news, _, contact = rest.partition("RECRUITING CONTACT:")
    return {"summary": summary.replace("SUMMARY:", "").strip(), "recent_news": news.strip(), "sources": sources,
            **_parse_contact(contact)}


def save(conn, company, data):
    conn.execute(
        "INSERT INTO company_research (company_norm, summary, recent_news, sources, contact_email, contact_url, "
        "researched_at, contact_checked_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now')) "
        "ON CONFLICT(company_norm) DO UPDATE SET "
        "summary = excluded.summary, recent_news = excluded.recent_news, sources = excluded.sources, "
        "contact_email = excluded.contact_email, contact_url = excluded.contact_url, "
        "researched_at = excluded.researched_at, contact_checked_at = excluded.contact_checked_at",
        (dedup.normalize_company(company), data["summary"], data["recent_news"], json.dumps(data["sources"]),
         data.get("contact_email"), data.get("contact_url")))
    conn.commit()


def format_research(company, data):
    lines = [f"{company}\n", data["summary"]]
    if data["recent_news"]:
        lines += ["\nRecent news:", data["recent_news"]]
    if data.get("contact_email"):
        lines += [f"\nRecruiting contact: {data['contact_email']} (published at {data['contact_url']})"]
    if data["sources"]:
        lines += ["\nSources:"] + [f"• {s['title']}: {s['url']}" for s in data["sources"][:8]]
    return "\n".join(lines)
