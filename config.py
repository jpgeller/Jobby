"""Settings loaded from .env. Every other module imports from here, never from os.environ."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _get(key, default=""):
    return os.getenv(key, default).strip()


ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
# Live mode needs both the switch and a key; anything else falls back to mock.
LLM_MODE = "live" if _get("LLM_MODE", "mock").lower() == "live" and ANTHROPIC_API_KEY else "mock"
MODEL_FAST = _get("MODEL_FAST", "claude-haiku-5-5")
MODEL_SMART = _get("MODEL_SMART", "claude-sonnet-5-5")

TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_CHAT_ID = _get("TELEGRAM_ALLOWED_CHAT_ID")

TRACKER_BACKEND = _get("TRACKER_BACKEND", "sheets")
ACTIONS_BACKEND = _get("ACTIONS_BACKEND", "fo")
WAJO_MCP_URL = _get("WAJO_MCP_URL", "https://mcp.api.wajo.ai/fo")
WAJO_TOKEN = _get("WAJO_TOKEN")
FO_CALL_MINUTES = int(_get("FO_CALL_MINUTES", "60"))

GOOGLE_SHEET_ID = _get("GOOGLE_SHEET_ID")
GMAIL_SCOPES = [s.strip() for s in _get("GMAIL_SCOPES", "gmail.readonly,gmail.send").split(",") if s.strip()]
TIMEZONE = _get("TIMEZONE", "America/Los_Angeles")
POLL_MINUTES = int(_get("POLL_MINUTES", "5"))
OUTREACH_MAX_PER_WEEK = int(_get("OUTREACH_MAX_PER_WEEK", "3"))
# While true, approved outreach goes to your own address instead of the recruiter (spec: "send to my
# own address first"). Set OUTREACH_SEND_TO_SELF=false in .env only when you're ready for real sends.
OUTREACH_SEND_TO_SELF = _get("OUTREACH_SEND_TO_SELF", "true").lower() != "false"

DB_PATH = ROOT / _get("DB_PATH", "jobby.db")
PREP_DIR = ROOT / "prep"
