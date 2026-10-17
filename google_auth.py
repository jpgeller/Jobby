"""One Google login for Gmail, Calendar, and Sheets, cached in token.json."""
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

CREDENTIALS_FILE = config.ROOT / "credentials.json"
TOKEN_FILE = config.ROOT / "token.json"

SCOPES = [f"https://www.googleapis.com/auth/{s}" for s in config.GMAIL_SCOPES] + [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/spreadsheets",
]

_creds = None


def credentials():
    """Load token.json, refresh it, or open the browser for consent the first time."""
    global _creds
    if _creds and _creds.valid:
        return _creds
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) if TOKEN_FILE.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid or not creds.has_scopes(SCOPES):
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    _creds = creds
    return creds


def service(name, version):
    return build(name, version, credentials=credentials(), cache_discovery=False)


if __name__ == "__main__":
    credentials()
    print(f"Google login saved to {TOKEN_FILE.name}.")
