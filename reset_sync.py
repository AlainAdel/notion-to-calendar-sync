import os
import logging
import sys
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# Configuration
CALENDAR_ID = "primary"
SYNC_FILE = "synced_events.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Setup simple logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("reset_sync")

def authenticate_google():
    """Authenticate with Google Calendar."""
    creds = None
    if os.path.exists("token.json"):
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load token.json: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                logger.info("Token expired, logging in again...")
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=0)
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)

def main():
    logger.info("Starting safely reset process...")
    
    # 1. Google Calendar Cleanup
    try:
        service = authenticate_google()
        logger.info("Authenticated with Google Calendar.")
        
        events_to_delete = []
        page_token = None
        
        logger.info("Scanning for Notion-synced events (source=notion-sync)...")
        while True:
            events_result = service.events().list(
                calendarId=CALENDAR_ID,
                privateExtendedProperty=['source=notion-sync'],
                pageToken=page_token,
                singleEvents=True
            ).execute()
            
            events = events_result.get('items', [])
            events_to_delete.extend(events)
            
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        
        count = len(events_to_delete)
        if count == 0:
            logger.info("No synced events found in Google Calendar.")
        else:
            logger.info(f"Found {count} events to delete. Deleting now...")
            for i, event in enumerate(events_to_delete, 1):
                try:
                    service.events().delete(calendarId=CALENDAR_ID, eventId=event['id']).execute()
                    if i % 10 == 0:
                        logger.info(f"Deleted {i}/{count} events...")
                except Exception as e:
                    logger.error(f"Failed to delete event {event.get('id')}: {e}")
            logger.info("Calendar cleanup complete.")
            
    except Exception as e:
        logger.error(f"Failed during Calendar cleanup: {e}")
        return

    # 2. Local State Cleanup
    logger.info("Cleaning up local state file (synced_events.json)...")
    if os.path.exists(SYNC_FILE):
        try:
            os.remove(SYNC_FILE)
            logger.info(f"Successfully deleted {SYNC_FILE}")
            logger.info("This resets last_run and db_fingerprint.")
        except Exception as e:
            logger.error(f"Failed to delete {SYNC_FILE}: {e}")
    else:
        logger.info(f"{SYNC_FILE} not found, effectively already reset.")

    logger.info("Reset complete. The next run will be a fresh sync.")

if __name__ == "__main__":
    main()
