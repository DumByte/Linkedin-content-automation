import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from flask import Flask, jsonify, render_template, request

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.database import (
    get_all_posts,
    get_drafts,
    get_posts_by_status,
    init_db,
    update_post_status,
)

app = Flask(__name__)

# Initialize DB once at startup
init_db()


@app.route("/")
def index():
    drafts = get_drafts(limit=10)
    return render_template("index.html", drafts=drafts)


@app.route("/history")
def history():
    status_filter = request.args.get("status", "all")
    if status_filter == "all":
        posts = get_all_posts(limit=100)
    else:
        posts = get_posts_by_status(status_filter, limit=100)
    return render_template("history.html", posts=posts, current_status=status_filter)


@app.route("/api/drafts")
def api_drafts():
    drafts = get_drafts(limit=10)
    return jsonify(drafts)


@app.route("/api/posts/<int:post_id>/status", methods=["POST"])
def api_update_status(post_id):
    data = request.get_json()
    status = data.get("status")
    if status not in ("approved", "posted", "rejected", "draft"):
        return jsonify({"error": "Invalid status"}), 400
    update_post_status(post_id, status)
    return jsonify({"ok": True, "post_id": post_id, "status": status})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
