import json
from notion_to_gcal import get_notion_events  # replace with your filename (without .py)

events = get_notion_events()
synced_ids = [e['id'] for e in events]

with open('synced_events.json', 'w') as f:
    json.dump(synced_ids, f, indent=2)

print(f"✅ Initialized with {len(synced_ids)} Notion events.")
