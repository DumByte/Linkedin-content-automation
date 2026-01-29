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

            CREATE INDEX IF NOT EXISTS idx_content_published
                ON scanned_content(published_at);
            CREATE INDEX IF NOT EXISTS idx_content_url
                ON scanned_content(url);
            CREATE INDEX IF NOT EXISTS idx_posts_status
                ON generated_posts(status);
            CREATE INDEX IF NOT EXISTS idx_sources_type
                ON sources(source_type);
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
