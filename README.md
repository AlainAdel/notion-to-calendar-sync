# Notion → Google Calendar Sync

Small Python utility that syncs a Notion database into Google Calendar: it creates new events, updates changed ones, and removes Google events whose corresponding Notion pages were deleted. Rich page content is flattened into a readable event description.

## What’s here
- `notion_to_gcal.py` — main sync script.
- `helper_snippet.py` — optional helper to inspect Notion events; not required for normal runs.
- `synced_events.json` — mapping of Notion page IDs to Google event IDs (written by the sync).
- `token.json` — Google OAuth refresh token (created on first login).

## Requirements
- Python 3.9+.
- Dependencies: `notion-client`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`, `python-dateutil`, `flask`, `python-dotenv`.
- A Notion internal integration with access to the target database.
- A Google Cloud project with the Calendar API enabled and a `credentials.json` OAuth client (Desktop) downloaded.

## Setup
1) From this directory:
```
python -m venv .venv
source .venv/bin/activate
pip install notion-client google-auth google-auth-oauthlib google-api-python-client python-dateutil flask python-dotenv
```
2) Notion
   - Create an internal integration; copy the secret.
   - Share the target database with the integration.
   - Export environment variables (e.g. in your shell or a `.env`):
```
export NOTION_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```
   - Ensure the date property is named `Do Date` (or change `DATE_PROPERTY_NAME` in the script).
3) Google
   - Enable Calendar API, download `credentials.json` into this folder.
   - First run will open a browser to grant access; `token.json` will be written for reuse.
4) (Optional) Set a specific calendar by editing `CALENDAR_ID` in `notion_to_gcal.py`; default is `primary`.

## Running the sync (one-shot)
```
python3 notion_to_gcal.py
```
To preview changes without applying them (Dry Run):
```
python3 notion_to_gcal.py --dry-run
```
On each run:
- Reads all pages in the Notion database.
- Builds Calendar events (timed vs all-day handled automatically).
- Creates events missing from Google, updates changed ones, and deletes Google events whose Notion pages disappeared.
- Persists the Notion→Google mapping in `synced_events.json`.

## Smart Polling (Run Frequently)
The script now includes a "Smart Polling" mechanism. It first checks the Last Edited time of the Notion database. If it hasn't changed since the last successful sync, the script exits immediately (seconds).

**Recommendation:**
Instead of running every hour, you can now run the script every **5-10 minutes** on your NAS.
- IF no changes: Script exits in <2 seconds.
- IF changes: Script runs full sync.

To force a sync even if no changes are detected (checks logic, but respects safety guard):
```bash
python notion_to_gcal.py --force
```

### Safety Guard
The script prevents mass deletions. If Notion returns 0 events but Google has many (>10) synced, it assumes an API error and aborts to protect your calendar. 
To bypass this and force delete everything (e.g. if you genuinely cleared the Notion database), use:
```bash
python notion_to_gcal.py --force
```


## Webhook-triggered mode (no scheduler needed)
Environment (can be set in shell or `.env`):
```
NOTION_TOKEN=...
NOTION_DATABASE_ID=...
NOTION_WEBHOOK_SECRET=shared-secret-from-notion
WEBHOOK_PATH=/notion/webhook          # optional; default shown
WEBHOOK_PORT=8000                     # optional; default shown
```

Run the webhook listener (keeps running):
```
python webhook_server.py
```
- Exposes `POST ${WEBHOOK_PATH}` for Notion to call.
- Verifies `X-Notion-Signature` using `NOTION_WEBHOOK_SECRET`.
- Ignores events that do not reference the configured database.
- Triggers the sync asynchronously so the webhook responds quickly.

Notion webhook registration:
1. Create a Notion webhook (via the Notion developer UI/API) pointing to your public HTTPS URL plus `WEBHOOK_PATH`.
2. Use the same signing secret as `NOTION_WEBHOOK_SECRET`.
3. Grant the webhook integration access to the target database.

Synology reverse proxy (high level):
1. Run `python webhook_server.py` on Synology (or via a process manager like `pm2`).
2. In DSM: Control Panel → Login Portal → Advanced → Reverse Proxy → Create.
   - Source: HTTPS, your domain (or DDNS), port 443/8443.
   - Destination: `localhost` and `WEBHOOK_PORT`.
   - Add your TLS cert (Let’s Encrypt or imported).
3. Open the chosen HTTPS port on your router/firewall and forward to Synology.
4. Use the public HTTPS URL in the Notion webhook configuration.

## Notes and tips
- Event titles are prefixed with `‣` to distinguish synced entries.
- Page content (headings, bullets, todos) is condensed into the Calendar description for readability.
- If refresh tokens break, the script falls back to a fresh OAuth login and rewrites `token.json`.
- `helper_snippet.py` is mainly for quick inspection of Notion items; the main sync does not require it.

## Scheduling (optional legacy)
If you prefer polling, you can still schedule the one-shot script. Example cron (runs every 30 minutes):
```
*/30 * * * * cd "/Volumes/home/PARA/4 Archives/Scripts/Notion to Calendar" && source .venv/bin/activate && NOTION_TOKEN=... NOTION_DATABASE_ID=... python notion_to_gcal.py >> sync.log 2>&1
```

