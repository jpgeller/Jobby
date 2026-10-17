"""Company research with web search, cached per company for 7 days (spec Section 3).

fetch() only calls Claude, so it can run in a worker thread; cached() and save() use the
database and stay on the main thread."""
import json
from datetime import datetime, timedelta, timezone

import config
import dedup
import llm

CACHE_DAYS = 7

SYSTEM = """You research companies for a student preparing internship applications and interviews.
Search the web, then answer in exactly this format, plain text, no markdown headings:

SUMMARY:
<4-6 sentences: what the company does, main products and customers, and anything about the team or product area related to the role>

RECENT NEWS:
- <one line per item, newest first, at most 4, each from the last 12 months; write "- None found" if there is nothing>

Only state what your sources support. Never invent news."""


def cached(conn, company):
    """The cached research row if it is fresh, else None."""
    row = conn.execute("SELECT * FROM company_research WHERE company_norm = ?",
                       (dedup.normalize_company(company),)).fetchone()
    if not row:
        return None
    researched = datetime.fromisoformat(row["researched_at"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - researched > timedelta(days=CACHE_DAYS):
        return None
    return {"summary": row["summary"], "recent_news": row["recent_news"], "sources": json.loads(row["sources"] or "[]")}


def fetch(company, role=None):
    prompt = f"Company: {company}\nRole: {role or 'internship'}\n\nResearch this company for my interview prep."
    text, sources = llm.ask_claude_research(prompt, task="research_company", system=SYSTEM, model=config.MODEL_SMART)
    summary, _, news = text.partition("RECENT NEWS:")
    return {"summary": summary.replace("SUMMARY:", "").strip(), "recent_news": news.strip(), "sources": sources}


def save(conn, company, data):
    conn.execute(
        "INSERT INTO company_research (company_norm, summary, recent_news, sources, researched_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) ON CONFLICT(company_norm) DO UPDATE SET "
        "summary = excluded.summary, recent_news = excluded.recent_news, sources = excluded.sources, "
        "researched_at = excluded.researched_at",
        (dedup.normalize_company(company), data["summary"], data["recent_news"], json.dumps(data["sources"])))
    conn.commit()


def format_research(company, data):
    lines = [f"{company}\n", data["summary"]]
    if data["recent_news"]:
        lines += ["\nRecent news:", data["recent_news"]]
    if data["sources"]:
        lines += ["\nSources:"] + [f"• {s['title']}: {s['url']}" for s in data["sources"][:8]]
    return "\n".join(lines)
