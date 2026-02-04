import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from flask import Flask, jsonify, render_template, request

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.content_generator import ContentGenerator
from src.database import (
    get_all_posts,
    get_candidate,
    get_drafts,
    get_posts_by_status,
    get_ranked_candidates,
    get_recent_failures,
    get_rejected_articles,
    get_rejected_candidates,
    init_db,
    insert_post,
    reject_candidate,
    update_candidate_status,
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


@app.route("/rejected")
def rejected():
    articles = get_rejected_articles(limit=50)
    user_rejected = get_rejected_candidates(limit=50)
    return render_template("rejected.html", articles=articles, user_rejected=user_rejected)


@app.route("/source-health")
def source_health():
    failures = get_recent_failures(limit=50)
    return render_template("source_health.html", failures=failures)


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


@app.route("/candidates")
def candidates():
    items = get_ranked_candidates()
    return render_template("candidates.html", candidates=items)


@app.route("/api/candidates")
def api_candidates():
    items = get_ranked_candidates()
    return jsonify(items)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json()
    candidate_id = data.get("candidate_id")
    if not candidate_id:
        return jsonify({"error": "candidate_id is required"}), 400

    candidate = get_candidate(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    if candidate["status"] == "generated":
        return jsonify({"error": "Already generated", "post_id": candidate["generated_post_id"]}), 409

    # Allow retry: reset error status back to generating
    update_candidate_status(candidate_id, "generating")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        update_candidate_status(candidate_id, "error", error_message="ANTHROPIC_API_KEY not set")
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    try:
        generator = ContentGenerator(api_key=api_key)
        result = generator.generate_post({
            "title": candidate.get("title", ""),
            "content": candidate.get("content", ""),
            "url": candidate.get("url", ""),
            "author": candidate.get("author", ""),
            "source_name": candidate.get("source_name", ""),
            "source_type": candidate.get("source_type", "rss"),
            "category": candidate.get("category", ""),
        })

        post_id = insert_post(
            content_id=candidate["content_id"],
            source_summary=result["source_summary"],
            commentary=result["commentary"],
            full_post=result["full_post"],
        )

        update_candidate_status(candidate_id, "generated", generated_post_id=post_id)
        return jsonify({
            "ok": True,
            "post_id": post_id,
            "full_post": result["full_post"],
            "source_summary": result["source_summary"],
        })

    except Exception as e:
        error_msg = str(e)[:500]
        update_candidate_status(candidate_id, "error", error_message=error_msg)
        return jsonify({"error": error_msg}), 500


@app.route("/api/candidates/<int:candidate_id>/reject", methods=["POST"])
def api_reject_candidate(candidate_id):
    candidate = get_candidate(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404
    reject_candidate(candidate["content_id"])
    update_candidate_status(candidate_id, "rejected")
    return jsonify({"ok": True, "candidate_id": candidate_id})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
