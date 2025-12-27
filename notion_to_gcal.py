import os
import sys
import json
import hashlib
import logging
import argparse
import importlib.metadata

# Ensure importlib.metadata has packages_distributions on Python 3.8
try:  # pragma: no cover
    if not hasattr(importlib.metadata, "packages_distributions"):
        import importlib_metadata as _ilm_backport  # type: ignore
        sys.modules["importlib.metadata"] = _ilm_backport
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()
from notion_client import Client as NotionClient
from notion_client.errors import RequestTimeoutError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
import dateutil.parser
import notion_client


# ---------------- CONFIG ---------------- #
DATE_PROPERTY_NAME = "Do Date"
CALENDAR_ID = "primary"
SYNC_FILE = "synced_events.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Setup logging
class ConsoleFilter(logging.Filter):
    def filter(self, record):
        # Filter out noisy third-party logs from console
        return not (record.name.startswith("notion_client") or 
                    record.name.startswith("googleapiclient") or 
                    record.name.startswith("google") or 
                    record.name.startswith("urllib3"))

def setup_logging():
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Remove existing handlers to avoid duplicates if re-run
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # 1. File Handler (Detailed)
    file_handler = logging.FileHandler("notion_to_gcal.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # 2. Console Handler (Clean, Summary only)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    # Simple format for console (just the message)
    console_formatter = logging.Formatter("%(message)s") 
    console_handler.setFormatter(console_formatter)
    # Add filter to suppress library noise on console
    console_handler.addFilter(ConsoleFilter())
    root_logger.addHandler(console_handler)

    return logging.getLogger(__name__)

logger = setup_logging()

# ------------- HELPERS ------------------ #
def compute_event_hash(event):
    """Compute a deterministic hash of the event data relevant for sync."""
    # Create a string representation of relevant fields
    # Use deterministic sorting for dictionary keys if any
    data_str = f"v1-reminders|{event['title']}|{event['start']}|{event['end']}|{event['description']}"
    return hashlib.md5(data_str.encode("utf-8")).hexdigest()

def format_uuid(id_str):
    """Ensure UUID is formatted with dashes."""
    if not id_str: 
        return id_str
    if len(id_str) == 32 and "-" not in id_str:
        return f"{id_str[:8]}-{id_str[8:12]}-{id_str[12:16]}-{id_str[16:20]}-{id_str[20:]}"
    return id_str

# ------------- AUTH GOOGLE -------------- #
def authenticate_google():
    """
    Authenticate with Google Calendar, auto-recovering from bad refresh tokens.
    """
    creds = None
    if os.path.exists("token.json"):
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load token.json: {e}")

    def save(creds_obj):
        with open("token.json", "w") as token:
            token.write(creds_obj.to_json())

    def new_login():
        logger.info("Initiating new Google OAuth login...")
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        fresh_creds = flow.run_local_server(port=0)
        save(fresh_creds)
        return fresh_creds

    if not creds:
        return build("calendar", "v3", credentials=new_login())

    if creds.valid:
        return build("calendar", "v3", credentials=creds)

    try:
        creds.refresh(Request())
        save(creds)
    except RefreshError:
        logger.warning("Token expired and refresh failed. Re-authenticating.")
        creds = new_login()
    except Exception as e:
        logger.error(f"Unexpected auth error: {e}")
        creds = new_login()

    return build("calendar", "v3", credentials=creds)


# -------------- NOTION ------------------ #
def get_page_content(notion, page_id):
    """Fetch readable text content from a Notion page."""
    texts = []
    next_cursor = None

    try:
        while True:
            blocks = notion.blocks.children.list(
                block_id=page_id, start_cursor=next_cursor, page_size=50
            )

            for block in blocks.get("results", []):
                block_type = block.get("type")
                rich_text = block.get(block_type, {}).get("rich_text", [])

                if not rich_text:
                    continue

                text_content = "".join([t.get("plain_text", "") for t in rich_text])

                if block_type in ["bulleted_list_item", "numbered_list_item"]:
                    texts.append(f"• {text_content}")
                elif block_type.startswith("heading_"):
                    texts.append(f"\n{text_content.upper()}\n")
                elif block_type == "to_do":
                    checked = "✅" if block[block_type].get("checked") else "☐"
                    texts.append(f"{checked} {text_content}")
                elif block_type == "quote":
                    texts.append(f"> {text_content}")
                elif block_type == "callout":
                    icon = block[block_type].get("icon", {}).get("emoji", "💡")
                    texts.append(f"{icon} {text_content}")
                else:
                    texts.append(text_content)

            if not blocks.get("has_more"):
                break
            next_cursor = blocks.get("next_cursor")

    except RequestTimeoutError:
        logger.error(f"Timeout fetching content for {page_id}")
    except Exception as e:
        logger.error(f"Failed to fetch content for {page_id}: {e}")

    return "\n".join(texts).strip()


def fetch_pages_via_search(notion, database_id):
    """
    Fallback method to fetch pages using the Search API.
    Essential for environments where databases.query fails.
    """
    logger.info("Using Search API to find pages...")
    pages = []
    has_more = True
    next_cursor = None
    target_id_clean = database_id.replace("-", "")
    logger.info(f"Target DB ID (clean): {target_id_clean}") 

    while has_more:
        try:
            results = notion.search(
                filter={"property": "object", "value": "page"},
                start_cursor=next_cursor,
                page_size=100
            )
            
            for page in results.get("results", []):
                parent = page.get("parent", {})
                
                # Check directly for database_id regardless of type
                pid = parent.get("database_id", "").replace("-", "")

                if pid == target_id_clean:
                    pages.append(page)
                else:
                    # Log ONE mismatch to diagnose (if we end up with 0 pages)
                    if len(pages) == 0 and pid:
                         logger.info(f"Debug Mismatch: Page {page.get('id')} has parent {pid} (Target: {target_id_clean})")
            
            has_more = results.get("has_more", False)
            next_cursor = results.get("next_cursor")
        except Exception as e:
            logger.error(f"Search API failed: {e}")
            break
            
    return pages


def get_notion_events(notion_token, database_id):
    if not notion_token or not database_id:
        raise EnvironmentError("Missing NOTION_TOKEN or NOTION_DATABASE_ID.")

    notion = NotionClient(auth=notion_token)
    has_more = True
    next_cursor = None
    events = []

    logger.info("Fetching events from Notion...")
    
    # Try standard query first
    try:
        # We try to use the SDK's query method.
        # If it doesn't exist or fails (400), we jump to search fallback.
        if not hasattr(notion.databases, "query"):
             raise AttributeError("Client missing databases.query")

        # Standard Query Loop
        while has_more:
            query = notion.databases.query(
                database_id=database_id, 
                start_cursor=next_cursor if next_cursor else None,
            )
            raw_pages = query.get("results", [])
            for page in raw_pages:
                # Helper to process page (since we duplicates logic otherwise)
                event = _process_page(notion, page)
                if event:
                    events.append(event)

            has_more = query.get("has_more", False)
            next_cursor = query.get("next_cursor")

    except Exception as e:
        logger.warning(f"Standard database query failed ({e}). Switching to Search Fallback.")
        # Fallback: Fetch ALL pages via search and filter by DB ID
        raw_pages = fetch_pages_via_search(notion, database_id)
        for page in raw_pages:
             event = _process_page(notion, page)
             if event:
                 events.append(event)

    logger.info(f"Found {len(events)} events in Notion.")
    return events


def _process_page(notion, page):
    """Parses a Notion page object into an event dict."""
    try:
        props = page.get("properties", {})
        
        # Title
        title_prop = props.get("Name", {}).get("title", [])
        if title_prop:
            title = title_prop[0].get("plain_text") or "Untitled"
        else:
            title = "Untitled"

        # Date
        date_prop = props.get(DATE_PROPERTY_NAME, {}).get("date")
        if not date_prop:
            return None
        
        start = date_prop["start"]
        end = date_prop.get("end", start)

        # Content
        content = get_page_content(notion, page["id"])

        return {
            "id": page["id"],
            "title": title,
            "start": start,
            "end": end,
            "description": content,
        }
    except Exception as exc:
        logger.error(f"Failed to process page {page.get('id')}: {exc}")
        return None

    logger.info(f"Found {len(events)} events in Notion.")
    return events


def get_database_fingerprint(notion, database_id):
    """
    Computes a 'fingerprint' of the current state of the database.
    This includes ALL pages (IDs and last_edited_time).
    Any change (add, edit, delete) will change this fingerprint.
    """
    try:
        # Dictionary to store unique pages: id -> last_edited_time
        # This implicitly handles deduplication if we switch strategies or retry.
        page_tracker = {}
        
        has_more = True
        next_cursor = None
        
        logger.info("Computing database fingerprint (scanning metadata)...")

        # Use query if available, else search fallback (implemented inline for simplicity/speed)
        use_search = not hasattr(notion.databases, "query")
        
        while has_more:
            current_batch = []
            if not use_search:
                try:
                    query = notion.databases.query(
                        database_id=database_id,
                        start_cursor=next_cursor if next_cursor else None,
                        page_size=100
                        # Removed filter_properties as it causes 400 on query endpoint
                    )
                    current_batch = query.get("results", [])
                    has_more = query.get("has_more", False)
                    next_cursor = query.get("next_cursor")
                except Exception:
                    logger.warning("Database query failed during fingerprinting. Switching to Search fallback.")
                    use_search = True
                    continue
            
            if use_search:
                # Search fallback
                # Note: Search doesn't support filter_properties as cleanly in all versions, 
                # but we just grab what we get.
                db_clean = database_id.replace("-", "")
                query = notion.search(
                    filter={"property": "object", "value": "page"},
                    start_cursor=next_cursor,
                    page_size=100
                )
                
                for p in query.get("results", []):
                    # Manual DB filtering
                    pid = p.get("parent", {}).get("database_id", "").replace("-", "")
                    if pid == db_clean:
                        current_batch.append(p)
                
                has_more = query.get("has_more", False)
                next_cursor = query.get("next_cursor")

            # Process batch
            for page in current_batch:
                # 1. STRICTLY ignore archived/deleted pages
                if page.get("archived"):
                    continue
                
                p_id = page["id"]
                p_time = page["last_edited_time"]
                
                # 2. Store in dictionary (deduplicates automatically)
                page_tracker[p_id] = p_time

        # 3. Create deterministic string
        # Sort by ID to ensure order
        sorted_ids = sorted(page_tracker.keys())
        
        lines = [f"COUNT:{len(sorted_ids)}"]
        for p_id in sorted_ids:
            lines.append(f"{p_id}|{page_tracker[p_id]}")

        full_str = "\n".join(lines)
        return hashlib.md5(full_str.encode("utf-8")).hexdigest()

    except Exception as e:
        logger.warning(f"Failed to compute fingerprint: {e}")
        return None


# --------- Sync File Helpers ---------- #
def load_synced_events():
    if os.path.exists(SYNC_FILE):
        try:
            with open(SYNC_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("Corrupt synced_events.json, starting fresh.")
            return {"events": {}, "last_run": None}
    return {"events": {}, "last_run": None}


def save_synced_events(data, dry_run=False):
    if dry_run:
        logger.info("[Dry Run] Would save synced_events.json")
        return
    with open(SYNC_FILE, "w") as f:
        json.dump(data, f, indent=2)


# -------- Clean Logging -------- #
def get_clean_logger():
    """
    Returns a logger that writes ONLY to sync.log with a specific format.
    It does NOT propagate to the root logger to avoid console spam.
    """
    clean_logger = logging.getLogger("clean_sync")
    clean_logger.setLevel(logging.INFO)
    clean_logger.propagate = False  # Don't send to root logger

    # Clear existing handlers to prevent duplicates
    if clean_logger.hasHandlers():
        clean_logger.handlers.clear()

    # File Handler
    file_handler = logging.FileHandler("sync.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    # Raw format - we control the timestamp/emojis manually
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    
    clean_logger.addHandler(file_handler)
    return clean_logger

# -------- Calendar Create/Update/Delete -------- #
def build_event_body(event):
    start_raw = event["start"]
    end_raw = event["end"] or event["start"]
    is_timed = "T" in start_raw

    if is_timed:
        if "T" not in end_raw:
            end_raw = start_raw
        start = {"dateTime": start_raw, "timeZone": "UTC"}
        end = {"dateTime": end_raw, "timeZone": "UTC"}
    else:
        start = {"date": start_raw}
        end = {"date": end_raw}

    return {
        "summary": f"‣ {event['title']}",
        "description": event["description"],
        "start": start,
        "end": end,
        "extendedProperties": {"private": {"source": "notion-sync"}},
        "reminders": {"useDefault": True},
    }


def execute_sync_plan(gcal, plan, clean_log, dry_run=False):
    """
    Executes the calculated sync plan.
    """
    synced = plan["synced_dict"]
    stats = {"created": 0, "updated": 0, "deleted": 0}

    # 1. Updates
    for item in plan["to_update"]:
        notion_id = item["notion_id"]
        event = item["event"]
        g_event_id = item["gcal_id"]
        current_hash = item["current_hash"]
        body = build_event_body(event)

        logger.info(f"Updating event: {event['title']}")
        if clean_log:
             clean_log.info(f"🔄 Updated: {event['title']}")

        if not dry_run:
            try:
                gcal.events().update(
                    calendarId=CALENDAR_ID,
                    eventId=g_event_id,
                    body=body,
                ).execute()
                synced[notion_id] = {"gcal_id": g_event_id, "hash": current_hash}
                stats["updated"] += 1
            except HttpError as e:
                # Handle 404 (Re-create)
                if e.resp.status == 404:
                    logger.warning(f"Event {g_event_id} not found (404). Re-creating.")
                    try:
                        created = gcal.events().insert(calendarId=CALENDAR_ID, body=body).execute()
                        synced[notion_id] = {"gcal_id": created["id"], "hash": current_hash}
                        stats["created"] += 1  # Count as create effectively
                    except Exception as ce:
                        logger.error(f"Failed to re-create {event['title']}: {ce}")
                else:
                    logger.error(f"Failed to update {event['title']}: {e}")
            except Exception as e:
                logger.error(f"Failed to update {event['title']}: {e}")
        else:
            logger.info(f"[Dry Run] Would update {g_event_id}")
            stats["updated"] += 1

    # 2. Creates
    for item in plan["to_create"]:
        notion_id = item["notion_id"]
        event = item["event"]
        current_hash = item["current_hash"]
        body = build_event_body(event)

        logger.info(f"Creating event: {event['title']}")
        if clean_log:
             clean_log.info(f"🆕 Created: {event['title']}")

        if not dry_run:
            try:
                created = gcal.events().insert(calendarId=CALENDAR_ID, body=body).execute()
                synced[notion_id] = {"gcal_id": created["id"], "hash": current_hash}
                stats["created"] += 1
            except Exception as e:
                 logger.error(f"Failed to create {event['title']}: {e}")
        else:
            logger.info(f"[Dry Run] Would create event")
            stats["created"] += 1

    # 3. Deletes
    for item in plan["to_delete"]:
        notion_id = item["notion_id"]
        g_id = item["gcal_id"]
        
        # We need the Title for the clean log, but we might not have it easily 
        # since it's deleted from Notion. We try our best or just say "ID".
        # For better UX, we could store titles in synced_events.json later.
        display_name = g_id 

        logger.info(f"Deleting event: {g_id}")
        if clean_log:
             clean_log.info(f"🗑️ Deleted event: {display_name}")

        if not dry_run:
            try:
                gcal.events().delete(calendarId=CALENDAR_ID, eventId=g_id).execute()
                del synced[notion_id]
                stats["deleted"] += 1
            except HttpError as e:
                if e.resp.status in [404, 410]:
                     # Already gone, just cleanup local
                     logger.warning(f"Event {g_id} already gone from Google (Status {e.resp.status}).")
                     del synced[notion_id]
                     stats["deleted"] += 1
                else: 
                     logger.error(f"Failed to delete {g_id}: {e}")
            except Exception as e:
                 logger.error(f"Failed to delete {g_id}: {e}")
        else:
             logger.info(f"[Dry Run] Would delete {g_id}")
             stats["deleted"] += 1

    return stats


def sync_events(gcal, notion_events, synced_data, clean_log=None, dry_run=False):
    synced = synced_data.get("events", {})
    # Backwards compatibility
    if "events" not in synced_data and synced_data:
         first_key = next(iter(synced_data))
         if isinstance(synced_data[first_key], (str, dict)):
             synced = synced_data
             synced_data["events"] = synced

    notion_ids = set()
    synced_copy = synced.copy()
    
    # --- PLAN PHASE ---
    plan = {
        "to_create": [],
        "to_update": [],
        "to_delete": [],
        "skipped_count": 0,
        "synced_dict": synced # Reference to update
    }

    # 1. Analyze Updates/Creates
    for event in notion_events:
        notion_id = event["id"]
        notion_ids.add(notion_id)
        current_hash = compute_event_hash(event)
        
        if notion_id in synced:
            sync_data = synced[notion_id]
            # Handle migration
            if isinstance(sync_data, str):
                g_event_id = sync_data
                stored_hash = None
            else:
                g_event_id = sync_data.get("gcal_id")
                stored_hash = sync_data.get("hash")

            if stored_hash == current_hash:
                plan["skipped_count"] += 1
                # Format update even if skipped
                if isinstance(sync_data, str):
                     synced[notion_id] = {"gcal_id": g_event_id, "hash": current_hash}
            else:
                plan["to_update"].append({
                    "notion_id": notion_id,
                    "event": event,
                    "gcal_id": g_event_id,
                    "current_hash": current_hash
                })
        else:
            plan["to_create"].append({
                "notion_id": notion_id,
                "event": event,
                "current_hash": current_hash
            })

    # 2. Analyze Deletes
    for notion_id, sync_data in synced_copy.items():
        if notion_id not in notion_ids:
             if isinstance(sync_data, str):
                 g_id = sync_data
             else:
                 g_id = sync_data.get("gcal_id")
             
             plan["to_delete"].append({
                 "notion_id": notion_id,
                 "gcal_id": g_id
             })

    # --- REPORT PHASE ---
    # Calculate totals
    count_create = len(plan["to_create"])
    count_update = len(plan["to_update"])
    count_delete = len(plan["to_delete"])
    count_skip = plan["skipped_count"]
    total_found = len(notion_events)

    logger.info(f"Plan: Create {count_create}, Update {count_update}, Delete {count_delete}, Skip {count_skip}")

    if clean_log:
        # User requested format:
        # Mon Dec 22 09:00:01 CET 2025: Running Notion to Calendar sync...
        # 📅 Found x events in Notion.
        # Updated: y, Created: z, Deleted: a, Skipped (Unchanged): b
        
        import time
        # We need a custom date string as requested
        # Note: Timezone name (CET/CEST) is tricky in standard python without pytz, 
        # using standard Ctime or strftime
        date_str = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        
        clean_log.info(f"{date_str}: Running Notion to Calendar sync...")
        clean_log.info(f"📅 Found {total_found} events in Notion.")
        clean_log.info(f"Updated: {count_update}, Created: {count_create}, Deleted: {count_delete}, Skipped (Unchanged): {count_skip}")

    # --- EXECUTE PHASE ---
    stats = execute_sync_plan(gcal, plan, clean_log, dry_run)
    
    # Save
    save_synced_events(synced_data, dry_run=dry_run)
    
    logger.info("--------------------------------------------------")
    logger.info(f"Sync Complete. Stats: {stats}")
    
    if clean_log:
        clean_log.info("✅ Sync complete.\n")


# ---------------- MAIN ---------------- #
# ---------------- RUNNER ---------------- #
def run_sync(dry_run=False, force=False):
    """
    Main entry point for syncing. 
    Returns True if sync actually ran, False if skipped.
    """
    if dry_run:
        logger.info("Running in DRY RUN mode -----------------")

    # Env vars
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DATABASE_ID")

    if not token or not db_id:
        logger.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID environment variables.")
        # Mask the database ID for logging if it exists, otherwise log a placeholder
        masked_id = db_id[:4] + "..." + db_id[-4:] if db_id and len(db_id) > 8 else db_id or "N/A"
        logger.info(f"Loaded Database ID: {masked_id}")
        return False
        
    try:
        import notion_client
        logger.info(f"Notion Client Version: {notion_client.__version__}")
    except Exception:
        logger.warning("Could not determine notion_client version.")

    try:
        # Initialize Notion
        notion = NotionClient(auth=token)

        # 1. Smart Polling Check (Fingerprint)
        synced_data = load_synced_events()
        last_fingerprint = synced_data.get("db_fingerprint")
        current_fingerprint = get_database_fingerprint(notion, db_id)

        if not force and not dry_run and last_fingerprint and current_fingerprint:
            # If fingerprint matches, NO changes (including deletions) occurred.
            if current_fingerprint == last_fingerprint:
                logger.info("Database fingerprint unchanged. Skipping sync.")
                return False
            else:
                 logger.info(f"Change detected (Fingerprint {last_fingerprint[:8]}... -> {current_fingerprint[:8]}...). Syncing...")

        # 2. Authenticate Google
        # In dry run, we still auth to ensure credentials work, unless we want to be totally offline.
        gcal = authenticate_google()

        # 3. Fetch Events
        notion_events = get_notion_events(token, db_id)
        
        # --- SAFETY GUARD ---
        # If Notion returns 0 events but we have many synced events (e.g. >10), 
        # it might be an API error or misconfig. Prevent mass deletion.
        synced_count = len(synced_data.get("events", {}))
        if len(notion_events) == 0 and synced_count > 10 and not force:
            # We use 'force' here as a proxy for "I know what I'm doing" (force-delete), 
            # or we can add a specific flag. Let's start with a warning and hard stop.
            logger.error(f"SAFETY GUARD: Notion returned 0 events, but you have {synced_count} currently synced.")
            logger.error("This looks like an anomaly. To convert these deletions, run with --force.")
            return False
            
        # 4. Sync
        clean_log = get_clean_logger()
        sync_events(gcal, notion_events, synced_data, clean_log=clean_log, dry_run=dry_run)

        # 5. Update last_run/fingerprint if successful and not dry-run
        if not dry_run and current_fingerprint:
            synced_data["db_fingerprint"] = current_fingerprint
            # We update last_run just for human reference
            from datetime import datetime
            synced_data["last_run"] = datetime.now().isoformat()
            save_synced_events(synced_data)
        
        return True

    except Exception as e:
        logger.exception(f"Fatal error during sync: {e}")
        raise e

def main():
    parser = argparse.ArgumentParser(description="Sync Notion events to Google Calendar")
    parser.add_argument("--dry-run", action="store_true", help="Simulate sync without modifying Google Calendar")
    parser.add_argument("--force", action="store_true", help="Force sync even if no changes detected")
    args = parser.parse_args()

    try:
        run_sync(dry_run=args.dry_run, force=args.force)
    except KeyboardInterrupt:
        logger.info("Sync cancelled by user.")
    except Exception:
        sys.exit(1)

if __name__ == "__main__":
    main()
