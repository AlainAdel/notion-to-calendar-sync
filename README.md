# Notion → Google Calendar Sync

Small Python utility that syncs a Notion database into Google Calendar: it creates new events, updates changed ones, and removes Google events whose corresponding Notion pages were deleted. Rich page content is flattened into a readable event description.

## What’s here
- `notion_to_gcal.py` — main sync script.
- `helper_snippet.py` — optional helper to inspect Notion events; not required for normal runs.
- `synced_events.json` — mapping of Notion page IDs to Google event IDs (written by the sync).
- `token.json` — Google OAuth refresh token (created on first login).

## Requirements
- Python 3.9+.
- Dependencies: `notion-client`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`, `python-dateutil`.
- A Notion internal integration with access to the target database.
- A Google Cloud project with the Calendar API enabled and a `credentials.json` OAuth client (Desktop) downloaded.

## Setup
1) From this directory:
```
python -m venv .venv
source .venv/bin/activate
pip install notion-client google-auth google-auth-oauthlib google-api-python-client python-dateutil
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

## Running the sync
```
python notion_to_gcal.py
```
On each run:
- Reads all pages in the Notion database.
- Builds Calendar events (timed vs all-day handled automatically).
- Creates events missing from Google, updates changed ones, and deletes Google events whose Notion pages disappeared.
- Persists the Notion→Google mapping in `synced_events.json`.

## Notes and tips
- Event titles are prefixed with `‣` to distinguish synced entries.
- Page content (headings, bullets, todos) is condensed into the Calendar description for readability.
- If refresh tokens break, the script falls back to a fresh OAuth login and rewrites `token.json`.
- `helper_snippet.py` is mainly for quick inspection of Notion items; the main sync does not require it.

## Scheduling
To keep calendars up to date, run via cron/launchd/systemd. Example cron (runs every 30 minutes):
```
*/30 * * * * cd "/Volumes/home/PARA/4 Archives/Scripts/Notion to Calendar" && source .venv/bin/activate && NOTION_TOKEN=... NOTION_DATABASE_ID=... python notion_to_gcal.py >> sync.log 2>&1
```
# Notion → Google Calendar Sync

Small Python utility that syncs a Notion database into Google Calendar: it creates new events, updates changed ones, and removes Google events whose corresponding Notion pages were deleted. Rich page content is flattened into a readable event description.

## What’s here
- `Notion to Calendar/notion_to_gcal.py` — main sync script.
- `Notion to Calendar/helper_snippet.py` — optional helper to inspect Notion events; not required for normal runs.
- `Notion to Calendar/synced_events.json` — mapping of Notion page IDs to Google event IDs (written by the sync).
- `Notion to Calendar/token.json` — Google OAuth refresh token (created on first login).

## Requirements
- Python 3.9+.
- Dependencies: `notion-client`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`, `python-dateutil`.
- A Notion internal integration with access to the target database.
- A Google Cloud project with the Calendar API enabled and a `credentials.json` OAuth client (Desktop) downloaded.

## Setup
1) From the project root:
```
cd "Notion to Calendar"
python -m venv .venv
source .venv/bin/activate
pip install notion-client google-auth google-auth-oauthlib google-api-python-client python-dateutil
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
   - Enable Calendar API, download `credentials.json` into `Notion to Calendar/`.
   - First run will open a browser to grant access; `token.json` will be written for reuse.
4) (Optional) Set a specific calendar by editing `CALENDAR_ID` in `notion_to_gcal.py`; default is `primary`.

## Running the sync
```
python notion_to_gcal.py
```
On each run:
- Reads all pages in the Notion database.
- Builds Calendar events (timed vs all-day handled automatically).
- Creates events missing from Google, updates changed ones, and deletes Google events whose Notion pages disappeared.
- Persists the Notion→Google mapping in `synced_events.json`.

## Notes and tips
- Event titles are prefixed with `‣` to distinguish synced entries.
- Page content (headings, bullets, todos) is condensed into the Calendar description for readability.
- If refresh tokens break, the script falls back to a fresh OAuth login and rewrites `token.json`.
- `helper_snippet.py` is mainly for quick inspection of Notion items; the main sync does not require it.

## Scheduling
To keep calendars up to date, run via cron/launchd/systemd. Example cron (runs every 30 minutes):
```
*/30 * * * * cd "/Volumes/home/PARA/4 Archives/Scripts/Notion to Calendar" && source .venv/bin/activate && NOTION_TOKEN=... NOTION_DATABASE_ID=... python notion_to_gcal.py >> sync.log 2>&1
```

