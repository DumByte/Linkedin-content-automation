import sqlite3
import os
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "content_history.db")


def get_db_path():
    return os.environ.get("DATABASE_PATH", DB_PATH)


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT UNIQUE NOT NULL,
                source_type TEXT NOT NULL,
                category TEXT,
                priority INTEGER DEFAULT 5,
                active BOOLEAN DEFAULT 1,
                last_scanned TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scanned_content (
                id INTEGER PRIMARY KEY,
                source_id INTEGER REFERENCES sources(id),
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                content TEXT,
                author TEXT,
                published_at TIMESTAMP,
                engagement_score REAL,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                selected BOOLEAN DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS generated_posts (
                id INTEGER PRIMARY KEY,
                content_id INTEGER REFERENCES scanned_content(id),
                source_summary TEXT,
                commentary TEXT,
                full_post TEXT,
                status TEXT DEFAULT 'draft',
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                posted_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rejected_articles (
                id INTEGER PRIMARY KEY,
                run_date TEXT NOT NULL,
                content_id INTEGER REFERENCES scanned_content(id),
                title TEXT,
                url TEXT,
                source_name TEXT,
                total_score REAL,
                recency_score REAL,
                substance_score REAL,
                authority_score REAL,
                engagement_score REAL,
                rejection_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS source_failures (
                id INTEGER PRIMARY KEY,
                source_id INTEGER REFERENCES sources(id),
                source_name TEXT,
                source_url TEXT,
                failure_type TEXT NOT NULL,
                error_message TEXT,
                consecutive_zero_count INTEGER DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_content_published
                ON scanned_content(published_at);
            CREATE INDEX IF NOT EXISTS idx_content_url
                ON scanned_content(url);
            CREATE INDEX IF NOT EXISTS idx_content_scanned_at
                ON scanned_content(scanned_at);
            CREATE INDEX IF NOT EXISTS idx_posts_status
                ON generated_posts(status);
            CREATE INDEX IF NOT EXISTS idx_sources_type
                ON sources(source_type);
            CREATE INDEX IF NOT EXISTS idx_rejected_run_date
                ON rejected_articles(run_date);
            CREATE INDEX IF NOT EXISTS idx_source_failures_recorded
                ON source_failures(recorded_at);

            CREATE TABLE IF NOT EXISTS ranked_candidates (
                id INTEGER PRIMARY KEY,
                run_date TEXT NOT NULL,
                content_id INTEGER REFERENCES scanned_content(id),
                title TEXT,
                url TEXT,
                source_name TEXT,
                category TEXT,
                total_score REAL,
                recency_score REAL,
                substance_score REAL,
                authority_score REAL,
                engagement_score REAL,
                status TEXT DEFAULT 'candidate',
                generated_post_id INTEGER,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_run_date
                ON ranked_candidates(run_date);
            CREATE INDEX IF NOT EXISTS idx_candidates_status
                ON ranked_candidates(status);

            CREATE TABLE IF NOT EXISTS candidate_rejections (
                id INTEGER PRIMARY KEY,
                content_id INTEGER UNIQUE REFERENCES scanned_content(id),
                rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_rejections_content_id
                ON candidate_rejections(content_id);
        """)


# --- Source CRUD ---

def upsert_source(name, url, source_type, category=None, priority=5):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO sources (name, url, source_type, category, priority)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                   name=excluded.name,
                   source_type=excluded.source_type,
                   category=excluded.category,
                   priority=excluded.priority""",
            (name, url, source_type, category, priority),
        )


def deactivate_sources_not_in(urls: set):
    """Deactivate any RSS sources whose URL is not in the given set."""
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in urls)
        conn.execute(
            f"UPDATE sources SET active=0 WHERE source_type='rss' AND url NOT IN ({placeholders})",
            list(urls),
        )


def get_active_sources(source_type=None):
    with get_connection() as conn:
        if source_type:
            rows = conn.execute(
                "SELECT * FROM sources WHERE active=1 AND source_type=? ORDER BY priority DESC",
                (source_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE active=1 ORDER BY priority DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def update_source_last_scanned(source_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE sources SET last_scanned=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), source_id),
        )


# --- Scanned Content CRUD ---

def insert_content(source_id, url, title, content, author=None, published_at=None):
    """Insert content, returns id or None if duplicate."""
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO scanned_content
                   (source_id, url, title, content, author, published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_id, url, title, content, author, published_at),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def content_exists(url):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM scanned_content WHERE url=?", (url,)
        ).fetchone()
        return row is not None


def get_recent_content(hours=48, limit=100):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT sc.*, s.name as source_name, s.source_type, s.category, s.priority
               FROM scanned_content sc
               JOIN sources s ON sc.source_id = s.id
               WHERE sc.scanned_at >= datetime('now', ? || ' hours')
               ORDER BY sc.scanned_at DESC
               LIMIT ?""",
            (f"-{hours}", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_candidate_pool(days=5):
    """Return all articles from the last N days that haven't been generated or rejected.

    This creates a rolling pool where articles persist across multiple days until:
    - A post is generated from them (any status in generated_posts)
    - They are explicitly rejected by the user (in candidate_rejections)
    - They age out (older than ``days`` calendar days)
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT sc.*, s.name as source_name, s.source_type,
                      s.category, s.priority
               FROM scanned_content sc
               JOIN sources s ON sc.source_id = s.id
               LEFT JOIN generated_posts gp ON gp.content_id = sc.id
               LEFT JOIN candidate_rejections cr ON cr.content_id = sc.id
               WHERE sc.scanned_at >= datetime('now', ? || ' days')
                 AND gp.id IS NULL
                 AND cr.id IS NULL
               ORDER BY sc.scanned_at DESC""",
            (f"-{days}",),
        ).fetchall()
        return [dict(r) for r in rows]


def reject_candidate(content_id):
    """Permanently reject a candidate so it won't appear in future pools."""
    with get_connection() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO candidate_rejections (content_id) VALUES (?)""",
            (content_id,),
        )


def get_rejected_candidates(limit=50):
    """Return user-rejected candidates with source info."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT sc.id as content_id, sc.title, sc.url, sc.scanned_at,
                      s.name as source_name, cr.rejected_at
               FROM candidate_rejections cr
               JOIN scanned_content sc ON cr.content_id = sc.id
               JOIN sources s ON sc.source_id = s.id
               ORDER BY cr.rejected_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_content_selected(content_ids):
    if not content_ids:
        return
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in content_ids)
        conn.execute(
            f"UPDATE scanned_content SET selected=1 WHERE id IN ({placeholders})",
            content_ids,
        )


# --- Generated Posts CRUD ---

def insert_post(content_id, source_summary, commentary, full_post):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO generated_posts
               (content_id, source_summary, commentary, full_post)
               VALUES (?, ?, ?, ?)""",
            (content_id, source_summary, commentary, full_post),
        )
        return cur.lastrowid


def get_drafts(limit=10):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT gp.*, sc.url as source_url, sc.title as source_title,
                      sc.author, s.name as source_name, s.category
               FROM generated_posts gp
               JOIN scanned_content sc ON gp.content_id = sc.id
               JOIN sources s ON sc.source_id = s.id
               WHERE gp.status = 'draft'
               ORDER BY gp.generated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_posts_by_status(status, limit=50):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT gp.*, sc.url as source_url, sc.title as source_title,
                      sc.author, s.name as source_name, s.category
               FROM generated_posts gp
               JOIN scanned_content sc ON gp.content_id = sc.id
               JOIN sources s ON sc.source_id = s.id
               WHERE gp.status = ?
               ORDER BY gp.generated_at DESC
               LIMIT ?""",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def update_post_status(post_id, status):
    with get_connection() as conn:
        now = datetime.now(timezone.utc).isoformat()
        if status == "approved":
            conn.execute(
                "UPDATE generated_posts SET status=?, approved_at=? WHERE id=?",
                (status, now, post_id),
            )
        elif status == "posted":
            conn.execute(
                "UPDATE generated_posts SET status=?, posted_at=? WHERE id=?",
                (status, now, post_id),
            )
        else:
            conn.execute(
                "UPDATE generated_posts SET status=? WHERE id=?",
                (status, post_id),
            )


def get_all_posts(limit=100):
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT gp.*, sc.url as source_url, sc.title as source_title,
                      sc.author, s.name as source_name, s.category
               FROM generated_posts gp
               JOIN scanned_content sc ON gp.content_id = sc.id
               JOIN sources s ON sc.source_id = s.id
               ORDER BY gp.generated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Rejected Articles CRUD ---

def insert_rejected_articles(run_date: str, articles: list[dict]):
    """Store rejected articles from a ranking run."""
    with get_connection() as conn:
        # Clear previous entries for the same run date
        conn.execute("DELETE FROM rejected_articles WHERE run_date = ?", (run_date,))
        for article in articles:
            breakdown = article.get("score_breakdown", {})
            conn.execute(
                """INSERT INTO rejected_articles
                   (run_date, content_id, title, url, source_name,
                    total_score, recency_score, substance_score,
                    authority_score, engagement_score, rejection_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_date,
                    article.get("id"),
                    article.get("title", ""),
                    article.get("url", ""),
                    article.get("source_name", ""),
                    article.get("engagement_score", 0),
                    breakdown.get("recency", 0),
                    breakdown.get("substance", 0),
                    breakdown.get("authority", 0),
                    breakdown.get("engagement", 0),
                    article.get("rejection_reason", ""),
                ),
            )


def get_rejected_articles(limit=20):
    """Get the most recent rejected articles."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM rejected_articles
               ORDER BY run_date DESC, total_score DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Source Failures CRUD ---

def insert_source_failure(source_id, source_name, source_url, failure_type,
                          error_message="", consecutive_zero_count=0):
    """Log a source failure."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO source_failures
               (source_id, source_name, source_url, failure_type,
                error_message, consecutive_zero_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, source_name, source_url, failure_type,
             error_message, consecutive_zero_count),
        )


def get_consecutive_zero_count(source_id):
    """Get the most recent consecutive zero-result count for a source."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT consecutive_zero_count FROM source_failures
               WHERE source_id = ? AND failure_type = 'zero_results'
               ORDER BY recorded_at DESC LIMIT 1""",
            (source_id,),
        ).fetchone()
        return row["consecutive_zero_count"] if row else 0


def get_recent_failures(limit=50):
    """Get recent source failures for dashboard display."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM source_failures
               ORDER BY recorded_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Ranked Candidates CRUD ---

def insert_ranked_candidates(run_date: str, candidates: list[dict]):
    """Store ranked candidate articles, replacing any previous candidates."""
    with get_connection() as conn:
        conn.execute("DELETE FROM ranked_candidates")
        for candidate in candidates:
            breakdown = candidate.get("score_breakdown", {})
            conn.execute(
                """INSERT INTO ranked_candidates
                   (run_date, content_id, title, url, source_name, category,
                    total_score, recency_score, substance_score,
                    authority_score, engagement_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_date,
                    candidate.get("id"),
                    candidate.get("title", ""),
                    candidate.get("url", ""),
                    candidate.get("source_name", ""),
                    candidate.get("category", ""),
                    candidate.get("engagement_score", 0),
                    breakdown.get("recency", 0),
                    breakdown.get("substance", 0),
                    breakdown.get("authority", 0),
                    breakdown.get("engagement", 0),
                ),
            )


def get_ranked_candidates():
    """Return all candidates from the latest run, ordered by score DESC.

    Resets any stale 'generating' statuses to 'candidate'.
    """
    with get_connection() as conn:
        # Reset stale generating state (browser was closed mid-generation)
        conn.execute(
            "UPDATE ranked_candidates SET status = 'candidate' WHERE status = 'generating'"
        )
        rows = conn.execute(
            """SELECT rc.*, sc.content, sc.published_at,
                      gp.full_post as generated_post_text
               FROM ranked_candidates rc
               LEFT JOIN scanned_content sc ON rc.content_id = sc.id
               LEFT JOIN generated_posts gp ON rc.generated_post_id = gp.id
               ORDER BY rc.total_score DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_candidate(candidate_id):
    """Return a single candidate by ID with its content."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT rc.*, sc.content, sc.author, sc.published_at,
                      s.source_type, s.priority
               FROM ranked_candidates rc
               LEFT JOIN scanned_content sc ON rc.content_id = sc.id
               LEFT JOIN sources s ON sc.source_id = s.id
               WHERE rc.id = ?""",
            (candidate_id,),
        ).fetchone()
        return dict(row) if row else None


def update_candidate_status(candidate_id, status, generated_post_id=None,
                            error_message=None):
    """Update candidate status after a generation attempt."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE ranked_candidates
               SET status = ?, generated_post_id = ?, error_message = ?
               WHERE id = ?""",
            (status, generated_post_id, error_message, candidate_id),
        )
