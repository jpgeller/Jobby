from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/spreadsheets",
]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

gmail = build("gmail", "v1", credentials=creds)
res = gmail.users().messages().list(userId="me", q="handshake", maxResults=5).execute()
for m in res.get("messages", []):
    msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata",
                                       metadataHeaders=["Subject"]).execute()
    print("EMAIL:", msg["payload"]["headers"][0]["value"])

cal = build("calendar", "v3", credentials=creds)
print("CALENDAR OK:", len(cal.events().list(calendarId="primary", maxResults=3).execute().get("items", [])), "events found")

sheets = build("sheets", "v4", credentials=creds)
sheet = sheets.spreadsheets().create(body={"properties": {"title": "Jobby test"}}).execute()
print("SHEET OK:", sheet["spreadsheetUrl"])