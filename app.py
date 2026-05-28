import os
import time
import uuid
import logging

import firebase_admin
from firebase_admin import credentials, auth, db
from flask import Flask, request, jsonify
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Firebase Admin Init
# Render: set FIREBASE_SERVICE_ACCOUNT_JSON env var with the
#         full JSON content of your service account key.
#         Also set FIREBASE_DATABASE_URL.
# ─────────────────────────────────────────────────────────────
def init_firebase():
    if firebase_admin._apps:
        return

    database_url = os.environ.get("FIREBASE_DATABASE_URL")
    if not database_url:
        raise RuntimeError("FIREBASE_DATABASE_URL env var is not set.")

    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        import json
        sa_dict = json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)
    else:
        # Fallback: path to local service account file (dev only)
        sa_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "serviceAccountKey.json")
        cred = credentials.Certificate(sa_path)

    firebase_admin.initialize_app(cred, {"databaseURL": database_url})
    logger.info("Firebase Admin SDK initialized.")

init_firebase()

# ─────────────────────────────────────────────────────────────
# Flask App
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

MESSAGES_REF   = "chatup/messages"
MAX_FETCH      = 60     # latest N messages returned on initial load
MAX_MSG_LEN    = 500    # character limit


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def verify_token(request) -> dict | None:
    """Verify Firebase ID token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    id_token = auth_header.split("Bearer ", 1)[1]
    try:
        return auth.verify_id_token(id_token)
    except Exception as e:
        logger.warning("Token verification failed: %s", e)
        return None


def error(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "CHATUP ONLINE", "version": "2.0"}), 200


@app.route("/api/messages", methods=["GET"])
def get_messages():
    """
    Fetch messages.
    Query params:
      after=<message_id>  → only return messages newer than this ID
      limit=<int>         → max results (default MAX_FETCH)
    """
    limit    = min(int(request.args.get("limit", MAX_FETCH)), 200)
    after_id = request.args.get("after")

    ref  = db.reference(MESSAGES_REF)
    snap = ref.order_by_key().limit_to_last(limit).get()

    if not snap:
        return jsonify({"messages": []}), 200

    messages = [{"id": k, **v} for k, v in snap.items()]
    messages.sort(key=lambda m: m.get("timestamp", 0))

    if after_id:
        ids = [m["id"] for m in messages]
        if after_id in ids:
            idx = ids.index(after_id)
            messages = messages[idx + 1:]

    return jsonify({"messages": messages}), 200


@app.route("/api/messages", methods=["POST"])
def post_message():
    """
    Send a message. Requires a valid Firebase ID token.
    Body: { "text": "..." }
    """
    decoded = verify_token(request)
    if not decoded:
        return error("Unauthorized", 401)

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()

    if not text:
        return error("Message text is required.")
    if len(text) > MAX_MSG_LEN:
        return error(f"Message exceeds {MAX_MSG_LEN} characters.")

    msg_id = str(uuid.uuid4()).replace("-", "")[:20]
    payload = {
        "id":           msg_id,
        "text":         text,
        "uid":          decoded["uid"],
        "display_name": decoded.get("name", "Unknown"),
        "photo_url":    decoded.get("picture", ""),
        "email":        decoded.get("email", ""),
        "timestamp":    int(time.time() * 1000),  # ms epoch
    }

    db.reference(f"{MESSAGES_REF}/{msg_id}").set(payload)
    logger.info("Message from %s: %s…", payload["display_name"], text[:40])

    return jsonify({"success": True, "id": msg_id}), 201


@app.route("/api/messages/<msg_id>", methods=["DELETE"])
def delete_message(msg_id: str):
    """
    Delete a message. Only the original sender can delete.
    """
    decoded = verify_token(request)
    if not decoded:
        return error("Unauthorized", 401)

    ref  = db.reference(f"{MESSAGES_REF}/{msg_id}")
    snap = ref.get()

    if not snap:
        return error("Message not found.", 404)
    if snap.get("uid") != decoded["uid"]:
        return error("Forbidden — not your message.", 403)

    ref.delete()
    return jsonify({"success": True}), 200


# ─────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)