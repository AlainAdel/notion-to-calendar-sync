import os
import json
from notion_client import Client as NotionClient
from notion_client.errors import RequestTimeoutError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from datetime import datetime, timezone
import dateutil.parser

# ---------------- CONFIG ---------------- #
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

if not NOTION_TOKEN or not DATABASE_ID:
    raise EnvironmentError(
        "Set NOTION_TOKEN and NOTION_DATABASE_ID environment variables."
    )

DATE_PROPERTY_NAME = "Do Date"  # Notion date field name

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = "primary"
SYNC_FILE = "synced_events.json"
NOTION_TIMEOUT_MS = int(os.environ.get("NOTION_TIMEOUT_MS", "20000"))  # 20s default


# ------------- AUTH GOOGLE -------------- #
def authenticate_google():
    """
    Authenticate with Google Calendar, auto-recovering from bad refresh tokens.
    Forces a fresh login when refresh fails, and always persists the latest token.
    """

    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    def save(creds_obj):
        with open("token.json", "w") as token:
            token.write(creds_obj.to_json())

    def new_login():
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        fresh_creds = flow.run_local_server(port=0)
        save(fresh_creds)
        return fresh_creds

    # No token yet: do interactive login
    if not creds:
        return build("calendar", "v3", credentials=new_login())

    # Token is already valid
    if creds.valid:
        return build("calendar", "v3", credentials=creds)

    # Attempt refresh, fallback to new login on failure
    try:
        creds.refresh(Request())
        save(creds)
    except RefreshError:
        creds = new_login()

    return build("calendar", "v3", credentials=creds)


# -------------- NOTION ------------------ #
def get_page_content(notion, page_id):
    """Fetch readable text content from a Notion page (paragraphs, lists, headings, etc.)."""
    texts = []
    next_cursor = None

    try:
        while True:
            # Paginate to avoid huge responses hanging the request
            blocks = notion.blocks.children.list(
                block_id=page_id, start_cursor=next_cursor, page_size=50
            )

            for block in blocks.get("results", []):
                block_type = block.get("type")
                rich_text = block.get(block_type, {}).get("rich_text", [])

                if not rich_text:
                    continue

                # Join all text fragments in the block
                text_content = "".join([t.get("plain_text", "") for t in rich_text])

                # Add bullet/heading markers for readability
                if block_type in ["bulleted_list_item", "numbered_list_item"]:
                    texts.append(f"• {text_content}")
                elif block_type.startswith("heading_"):
                    texts.append(f"\n{text_content.upper()}\n")
                elif block_type == "to_do":
                    checked = "✅" if block[block_type].get("checked") else "☐"
                    texts.append(f"{checked} {text_content}")
                else:
                    texts.append(text_content)

            if not blocks.get("has_more"):
                break
            next_cursor = blocks.get("next_cursor")

    except RequestTimeoutError:
        print(f"⏱️ Timeout while fetching page content for {page_id}")
    except Exception as e:
        print(f"⚠️ Failed to fetch page content for {page_id}: {e}")

    return "\n".join(texts).strip()


def get_notion_events():
    notion = NotionClient(auth=NOTION_TOKEN, timeout_ms=NOTION_TIMEOUT_MS)
    has_more = True
    next_cursor = None
    events = []

    while has_more:
        query = notion.databases.query(
            database_id=DATABASE_ID, start_cursor=next_cursor
        )
        for page in query["results"]:
            props = page["properties"]

            # Title
            title_prop = props.get("Name", {}).get("title", [])
            if title_prop:
                first = title_prop[0]
                title = first.get("plain_text") or first.get("text", {}).get("content")
            else:
                title = "Untitled"

            # Date
            date_prop = props.get(DATE_PROPERTY_NAME, {}).get("date")
            if not date_prop:
                continue
            start = date_prop["start"]
            end = date_prop.get("end", start)

            # Page content
            content = get_page_content(notion, page["id"])

            events.append(
                {
                    "id": page["id"],
                    "title": title,
                    "start": start,
                    "end": end,
                    "description": content,
                }
            )

        has_more = query.get("has_more", False)
        next_cursor = query.get("next_cursor")

    print(f"📅 Found {len(events)} events in Notion.")
    return events


# --------- Sync File Helpers ---------- #
def load_synced_events():
    if os.path.exists(SYNC_FILE):
        with open(SYNC_FILE, "r") as f:
            return json.load(f)
    return {}


def save_synced_events(data):
    with open(SYNC_FILE, "w") as f:
        json.dump(data, f, indent=2)


# -------- Calendar Create/Update/Delete -------- #
def build_event_body(event):
    start_raw = event["start"]
    end_raw = event["end"] or event["start"]

    # Ensure both start and end have the same type (date or dateTime)
    is_timed = "T" in start_raw

    # If Notion gives mismatched formats, normalize both
    if is_timed:
        if "T" not in end_raw:
            # convert end date-only to same day with +1h
            end_raw = start_raw
        start = {"dateTime": start_raw, "timeZone": "UTC"}
        end = {"dateTime": end_raw, "timeZone": "UTC"}
    else:
        # all-day event (date only)
        start = {"date": start_raw}
        end = {"date": end_raw}

    return {
        "summary": f"‣ {event['title']}",
        "description": event["description"],
        "start": start,
        "end": end,
        "extendedProperties": {"private": {"source": "notion-sync"}},
    }


def sync_events(gcal, notion_events, synced):
    notion_ids = [e["id"] for e in notion_events]
    synced_copy = synced.copy()

    def get_field(event_dict):
        """Return either 'dateTime' or 'date' value from a Google/Notion event start/end field."""
        if not isinstance(event_dict, dict):
            return ""
        if "dateTime" in event_dict:
            return event_dict["dateTime"]
        elif "date" in event_dict:
            return event_dict["date"]
        return ""

    # --- Create or Update ---
    for event in notion_events:
        body = build_event_body(event)
        notion_id = event["id"]

        if notion_id in synced:
            g_event_id = synced[notion_id]
            try:
                g_event = gcal.events().get(
                    calendarId=CALENDAR_ID, eventId=g_event_id
                ).execute()

                # Compare and update if different
                changed = False

                # Compare only meaningful fields
                # def normalize(dt_str):
                #     """Normalize ISO date/time strings for accurate comparison (ignores milliseconds, tz suffixes)."""
                #     if not dt_str:
                #         return ""
                #     dt_str = dt_str.strip().replace("Z", "+00:00")
                #     try:
                #         # Parse into datetime object to standardize
                #         dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                #         # Convert back to canonical ISO format (without milliseconds)
                #         return dt.replace(microsecond=0).isoformat()
                #     except Exception:
                #         # In case of date-only values
                #         return dt_str
                def normalize(dt_str):
                    """Normalize ISO date/time strings for accurate comparison (timezone-aware UTC, no microseconds)."""
                    if not dt_str:
                        return ""
                    try:
                        dt = dateutil.parser.isoparse(dt_str)
                        # Convert everything to UTC and remove microseconds
                        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
                        return dt.isoformat()
                    except Exception:
                        # If it's a date-only value, just return it unchanged
                        return dt_str

                start_changed = normalize(get_field(g_event.get("start"))) != normalize(get_field(body.get("start")))
                end_changed = normalize(get_field(g_event.get("end"))) != normalize(get_field(body.get("end")))
                summary_changed = g_event.get("summary") != body.get("summary")
                description_changed = g_event.get("description", "") != body.get("description")

                if start_changed or end_changed or summary_changed or description_changed:
                    print(f"🔍 Change detected in {event['title']}:")
                    if start_changed: print("   ↳ Start changed")
                    if end_changed: print("   ↳ End changed")
                    if summary_changed: print("   ↳ Title changed")
                    if description_changed: print("   ↳ Description changed")

                    try:
                        gcal.events().update(
                            calendarId=CALENDAR_ID,
                            eventId=g_event_id,
                            body=body,
                        ).execute()
                        print(f"🔄 Updated: {event['title']}")
                    except Exception as e:
                        print(f"⚠️ Failed to update {event['title']}: {e}")

            except Exception as e:
                print(f"⚠️ Failed to update {event['title']}: {e}")
        else:
            try:
                created = gcal.events().insert(
                    calendarId=CALENDAR_ID, body=body
                ).execute()
                synced[notion_id] = created["id"]
                print(f"🆕 Created: {event['title']}")
            except Exception as e:
                print(f"⚠️ Failed to create {event['title']}: {e}")

    # --- Delete missing ---
    for notion_id, g_id in list(synced_copy.items()):
        if notion_id not in notion_ids:
            try:
                gcal.events().delete(calendarId=CALENDAR_ID, eventId=g_id).execute()
                del synced[notion_id]
                print(f"🗑️ Deleted event: {g_id}")
            except Exception as e:
                print(f"⚠️ Failed to delete event {g_id}: {e}")

    save_synced_events(synced)


# ---------------- MAIN ---------------- #
def main():
    gcal = authenticate_google()
    notion_events = get_notion_events()
    synced = load_synced_events()
    sync_events(gcal, notion_events, synced)
    print("✅ Sync complete.")


if __name__ == "__main__":
    main()
