import hashlib
import hmac
import os
import threading
from typing import Any

from flask import Flask, abort, jsonify, request
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# Import the sync logic
from notion_to_gcal import run_sync

WEBHOOK_SECRET = os.environ.get("NOTION_WEBHOOK_SECRET")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/notion/webhook")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8000"))
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# Normalize the path so both "/path" and "path" are accepted.
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = f"/{WEBHOOK_PATH}"

app = Flask(__name__)


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate Notion webhook signature."""
    if not WEBHOOK_SECRET:
        print("⚠️ NOTION_WEBHOOK_SECRET is not set; rejecting webhook.")
        return False

    if not signature_header:
        print("⚠️ Missing X-Notion-Signature header.")
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        print("⚠️ Signature mismatch.")
        return False
    return True


def payload_targets_database(payload: Any) -> bool:
    """Check if the payload references our target database."""
    if not DATABASE_ID:
        # If we don't know the DB ID, we can't filter, so we assume yes? 
        # Or better to be safe and allow it, but sync will fail if env var missing there too.
        return True

    target = DATABASE_ID.replace("-", "")
    seen: set[str] = set()

    def walk(node: Any):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in {"database_id", "parent_id", "id"} and isinstance(v, str):
                    seen.add(v.replace("-", ""))
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return target in seen or target in str(payload).replace("-", "")


def trigger_sync_async():
    """Kick off the sync without blocking the webhook response."""

    def _runner():
        try:
            # We force api sync because the webhook told us there's a change
            print("🚀 Webhook triggered sync starting...")
            run_sync(force=True)
        except Exception as exc: 
            print(f"⚠️ Sync failed: {exc}")

    threading.Thread(target=_runner, daemon=True).start()


@app.route(WEBHOOK_PATH, methods=["POST"])
def notion_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Notion-Signature")

    if not verify_signature(raw_body, signature):
        abort(401)

    payload = request.get_json(silent=True) or {}

    if not payload_targets_database(payload):
        return jsonify({"status": "ignored", "reason": "different_database"}), 200

    trigger_sync_async()
    return jsonify({"status": "accepted"}), 202


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return "ok", 200


def run_server():
    """Start the webhook listener."""
    app.run(host="0.0.0.0", port=WEBHOOK_PORT)


if __name__ == "__main__":
    run_server()

