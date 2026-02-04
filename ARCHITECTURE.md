# ARCHITECTURE.md — LinkedIn Content Automation

**Last updated:** 2026-02-04
**Codebase version:** Post-enhancement (see Changelog at end)
**Maintainer context:** Single-developer project, pre-production

---

## 1. SYSTEM OVERVIEW

### 1.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     GitHub Actions (8AM UTC daily)                   │
│  ┌─────────────┐   ┌──────────┐   ┌───────────┐   ┌────────────┐  │
│  │ RSS Scanner  │──▶│  Ranker   │──▶│  SQLite DB  │               │  │
│  │ (feedparser) │   │ (scoring) │   │ (candidates)│               │  │
│  └─────────────┘   └──────────┘   └─────────────┘               │  │
└─────────────────────────────────────────────────────────────────────┘
                                                          │
                                            artifact upload/download
                                                          │
┌─────────────────────────────────────────────────────────────────────┐
│                     Render.com (always-on web service)               │
│  ┌──────────────┐   ┌───────────────────────────────────────────┐  │
│  │ Flask (app.py)│──▶│  Dashboard: select candidates, generate,   │  │
│  │  port 10000   │   │  review, approve, copy, track              │  │
│  └──────────────┘   └───────────────────────────────────────────┘  │
│         │                                                           │
│         ▼                                                           │
│  ┌────────────┐                                                     │
│  │ SQLite DB   │  (/data/content_history.db on persistent disk)     │
│  └────────────┘                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 Core Value Proposition

Automated pipeline that: (1) scans 100+ fintech/AI sources daily via RSS, (2) scores and ranks the top 20 candidate articles by recency, substance, authority, and engagement signals, (3) presents ranked candidates in a dashboard for manual selection, (4) generates LinkedIn post drafts on demand using Claude with strong anti-cringe guardrails, and (5) presents drafts for human approval before posting.

The system is designed for a single fintech professional who wants to maintain a consistent LinkedIn presence without spending time on content discovery or first-draft writing. The manual selection step gives the user control over which articles become posts.

### 1.3 Key Design Decisions and Rationale

| Decision | Rationale | Trade-off |
|---|---|---|
| **SQLite over Postgres** | Zero-ops for a single-user system; file-based persistence works with artifact storage and Render disks | No concurrent writes, no remote access, 1GB practical limit on free tier |
| **GitHub Actions for scanning** | Free CI/CD minutes, no server to maintain for batch jobs, artifact storage for DB persistence | No real-time scanning; 90-day artifact retention creates a data loss cliff |
| **Render.com for dashboard** | Free tier web service with persistent disk, auto-deploy from git | Free tier has spin-down (cold starts), 1GB disk limit |
| **Claude Sonnet (not Opus/Haiku)** | Sonnet balances quality and cost for 150-200 word posts; Opus would be overkill, Haiku too terse | ~$0.01-0.03 per post is acceptable for 3 posts/day |
| **Manual candidate selection** | User picks which of top 20 ranked articles to generate posts for, giving full editorial control | Requires manual dashboard interaction; no longer fully automated |
| **No LinkedIn API integration** | LinkedIn's API requires app approval and has restrictive terms; copy-to-clipboard is simpler | Manual posting step remains; no post-performance analytics |
| **RSS-only scanning (Twitter disabled)** | All free Twitter RSS bridges (Nitter, RSSHub) stopped working after API changes | 15 configured Twitter sources are effectively dead weight |

### 1.4 Known Limitations and Technical Debt

1. **Twitter scanner is dead code.** `TwitterScanner` exists but is disabled in `main.py:27-28`. All Nitter/RSSHub instances are defunct. The 15 `twitter_accounts` entries in `sources.json` are loaded into the DB but never scanned.

2. **No test suite.** Zero test files exist. The ranking algorithm, anti-cringe filter, and database operations are untested.

3. ~~**`sources.json` has unreachable sources.**~~ **MOSTLY FIXED:** Added browser User-Agent to feedparser requests (fixes ~25+ sources that were returning HTML to bot traffic), added SSL certificate fallback, fixed 7 incorrect feed URLs, and disabled 9 permanently broken sources (dead domains, discontinued feeds, non-RSS pages). Remaining paywalled sources (The Information) may still return limited data.

4. **`WebScraper` is never used.** It's defined in `src/scanners/web_scraper.py` but never instantiated in `main.py`. Only `RSSScanner` is used.

5. **No data sync between GitHub Actions and Render.** The scanning pipeline (GitHub Actions) and dashboard (Render) use separate SQLite files. There's no mechanism to sync the artifact DB to Render's persistent disk.

6. **Dashboard runs Flask dev server in production.** `app.py` uses `app.run()` directly. The `render.yaml` start command is `python dashboard/app.py`, not `gunicorn`. Despite `gunicorn` being in `requirements.txt`, it's unused.

7. **No input validation on the status update API.** `POST /api/posts/<int:post_id>/status` validates the status string but doesn't verify the post exists. Updating a nonexistent ID silently succeeds.

8. **Content deduplication uses `quick_ratio()`.** `SequenceMatcher.quick_ratio()` is an upper bound, not exact similarity. It can let near-duplicates through. See `ranker.py:189`.

9. **No database migrations.** Schema changes require manual intervention or DB recreation.

10. ~~**Regulatory and academic sources are loaded but silently ignored.**~~ **FIXED:** `load_sources_from_config()` now processes all 7 config sections including `regulatory`, `academic`, and `vc_blogs`.

---

## 2. DATA FLOW & PIPELINE

### 2.1 End-to-End Data Flow

```
sources.json ──▶ load_sources_from_config() ──▶ sources table (upsert)
                                                      │
                        ┌─────────────────────────────┘
                        ▼
              get_active_sources("rss")
                        │
                        ▼
              RSSScanner.scan_safe(source)  ── rate limit (1s) ──▶ feedparser.parse(url)
                        │                                              │
                        │                                    (on failure/zero results)
                        │                                              ▼
                        │                              insert_source_failure() ──▶ source_failures table
                        │                              _append_failure_log()  ──▶ data/source_failures.jsonl
                        ▼
              insert_content()  ──▶ scanned_content table (dedup by URL via UNIQUE)
                        │
                        ▼
              get_candidate_pool(days=5)  ──▶ fetch articles from last 5 days
                │   Excludes: articles with generated posts (any status)
                │   Excludes: articles explicitly rejected by user
                        │
                        ▼
              rank_content(items, top_n=20)
                │ 1. URL deduplication
                │ 2. Content similarity dedup (>80% via SequenceMatcher)
                │ 3. Score breakdown: recency(30) + substance(25) + authority(20) + engagement(25)
                │ 4. Sort descending, pick top 20 by score
                │ 5. Track rejected articles with reasons + score breakdowns
                        │
                        ├──▶ insert_rejected_articles()  ──▶ rejected_articles table
                        ▼
              insert_ranked_candidates()  ──▶ ranked_candidates table (top 20)
                        │
                        ▼  (pipeline stops here — manual step below)
                        │
              Dashboard /candidates: user reviews ranked articles
                        │
                        ▼  (user selects articles + clicks Generate)
                        │
              POST /api/generate  (per article, sequential)
                │ 1. ContentGenerator.generate_post(article)
                │ 2. Apply _anti_cringe_filter()
                │ 3. insert_post()  ──▶ generated_posts table (status='draft')
                │ 4. update_candidate_status()  ──▶ status='generated'
                        │
                        ▼
              Dashboard /: human reviews drafts, approves/rejects, copies to clipboard
```

### 2.2 State Transitions

```
                    ┌──────────────────────────────────────────┐
                    │           scanned_content                 │
                    │                                           │
  RSS Feed ──▶ [inserted] ──▶ ranked by scoring algorithm      │
                    └──────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │         ranked_candidates                  │
                    │                                           │
  Ranker ──▶ [candidate] ──▶ [generating] ──▶ [generated]     │
                    │              │                             │
                    │              └──▶ [error] ──▶ [candidate] │
                    │                    (retry resets status)   │
                    └──────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │           generated_posts                 │
                    │                                           │
  Claude API ──▶ [draft] ──▶ [approved] ──▶ [posted]          │
                    │            ▲                              │
                    │            │                              │
                    └──▶ [rejected]                             │
                    └──────────────────────────────────────────┘
```

Valid `ranked_candidates.status` values: `candidate`, `generating`, `generated`, `error`. On page load, stale `generating` states are reset to `candidate`. On retry, `error` is reset to `generating`.

Valid `generated_posts.status` values: `draft`, `approved`, `posted`, `rejected`. Transitions are enforced only by the dashboard API whitelist, not by database constraints. Any status can transition to any other status.

### 2.3 Data Persistence Strategy

| Data | Location | Retention | Notes |
|---|---|---|---|
| Scanned content | `scanned_content` table | Indefinite (no cleanup) | Grows ~50-200 rows/day |
| Generated posts | `generated_posts` table | Indefinite | ~3 rows/day |
| Sources | `sources` table | Indefinite | ~99 rows (static) |
| Ranked candidates | `ranked_candidates` table | Replaced each scan run | ~20 rows (latest run only) |
| Rejected articles | `rejected_articles` table | Overwritten per run date | ~20 rows/day |
| User rejections | `candidate_rejections` table | Indefinite (permanent) | Grows as user rejects candidates |
| Source failures | `source_failures` table | Indefinite | ~5-20 rows/day |
| Failure log | `data/source_failures.jsonl` | Indefinite (append-only) | One JSON line per hard failure |
| DB file (CI) | GitHub Actions artifact | **90 days** | `retention-days: 90` — silent data loss after 90 days of no runs |
| DB file (dashboard) | Render persistent disk | Until disk deleted | 1GB limit |

**Critical gap:** There is no mechanism to copy the GitHub Actions artifact to Render's persistent disk. These are two independent SQLite files. The dashboard on Render will only show posts from its own DB, which is only populated if `main.py` is run against it directly (e.g., via SSH or a separate cron on Render).

### 2.4 Failure Modes and Error Handling

| Stage | Failure Mode | Handling | Impact |
|---|---|---|---|
| RSS scan | Feed timeout/malformed | `scan_safe()` catches all exceptions, returns failure info; logged to `source_failures` table + `source_failures.jsonl` | Source tracked with failure type (timeout/http_error/parse_error) |
| RSS scan | Feed returns 0 entries | Logged as `zero_results` failure with consecutive count tracking; warning emitted after 3+ consecutive zeros | Persistent visibility into degraded sources |
| Content insert | Duplicate URL | `sqlite3.IntegrityError` caught, returns `None` | Duplicate silently skipped (correct behavior) |
| Ranking | No candidate articles in pool | `get_candidate_pool()` returns `[]`, ranking skipped | Run completes with no output |
| Claude API | Rate limit / server error | `anthropic.APIError` raised, logged, item skipped in batch | Other items in batch still generated |
| Claude API | Key missing | Logged as error, generation aborted | Run completes but produces no posts |
| Dashboard | DB not initialized | `init_db()` called at startup | Auto-creates tables |
| Dashboard | Post ID doesn't exist | Status update silently succeeds (UPDATE WHERE id=?) | No error returned to user |

---

## 3. COMPONENT ARCHITECTURE

### 3.1 Scanning Layer (`src/scanners/`)

#### Class Hierarchy

```
BaseScanner (ABC)
├── RSSScanner      ← Active, used in production
├── TwitterScanner  ← Disabled, dead code
└── WebScraper      ← Implemented, never instantiated
```

#### BaseScanner (`base_scanner.py`)

Provides the `ScannedItem` dataclass (the canonical data transfer object) and two shared behaviors:

```python
@dataclass
class ScannedItem:
    url: str                          # Unique identifier, used for dedup
    title: str                        # Article headline
    content: str                      # Extracted body text
    author: Optional[str] = None      # Byline if available
    published_at: Optional[str] = None  # ISO string, critical for ranking
    source_id: Optional[int] = None   # FK to sources table
    metadata: dict = field(default_factory=dict)  # Unused extensibility point
```

- **Rate limiting:** Token bucket with configurable interval (default 2s). Uses `time.sleep()` — blocks the thread. Not an issue for the current single-threaded design but would be a bottleneck with concurrent scanning.
- **Recency validation:** `is_recent()` rejects items older than 180 days. Uses `dateutil.parser.parse(fuzzy=True)` which is forgiving but can misinterpret ambiguous date strings.

#### RSSScanner (`rss_scanner.py`)

The only active scanner. Processing per source:

1. `_fetch_feed(url)` — `feedparser.parse(url, agent=USER_AGENT)` with browser-like User-Agent header, plus SSL certificate fallback for sites with cert issues
2. For each entry: parse date, check recency, extract content, build `ScannedItem`
3. Rate limit: **1 second** between sources (overrides base 2s default)

**User-Agent:** Sends a Chrome-like `User-Agent` string (`Mozilla/5.0 ... Chrome/131.0.0.0 ...`). Many sites (government agencies, news outlets, tech blogs) return HTML error pages or bot-detection challenges when they see feedparser's default user agent, causing XML parse failures. The browser User-Agent resolves this for the majority of sources.

**SSL Fallback:** If a feed fails with `CERTIFICATE_VERIFY_FAILED` (e.g., self-signed certs, hostname mismatches), the scanner retries with an unverified SSL context. This handles sites like ECB and 11:FS that have cert issues but still serve valid feeds.

Content extraction priority:
1. `entry.content[0].value` (rich content field from Atom feeds)
2. `entry.summary` (RSS 2.0 description)
3. `entry.description` (fallback)

HTML stripping is basic regex (`<[^>]+>` → space), not a proper HTML parser. This can leave artifacts from complex HTML (e.g., `&nbsp;`, malformed entities).

Content is capped at **5000 characters** per item.

**Feed error handling:** `feedparser` sets `feed.bozo = True` for malformed feeds but still populates `feed.entries` when possible. The scanner only treats it as failure when `bozo=True AND entries is empty`.

#### TwitterScanner (`twitter_scanner.py`) — DISABLED

Attempted strategy: proxy Twitter timelines through Nitter (privacy frontend) or RSSHub instances, both of which expose Twitter content as RSS feeds. Hardcoded 4 Nitter instances and 2 RSSHub instances to rotate through on failure.

**Why it's dead:** Twitter/X blocked all third-party scraping in 2023-2024. Every Nitter instance either shut down or became unreliable. RSSHub's Twitter route requires a paid API key. The code remains as a reference for if/when an alternative becomes available.

The scanner is explicitly commented out in `main.py:27-28` and `main.py:108-110`.

#### WebScraper (`web_scraper.py`) — IMPLEMENTED BUT UNUSED

Two-stage content extraction:

1. **Primary:** `trafilatura.extract()` with `favor_precision=True` and JSON output for metadata
2. **Fallback:** BeautifulSoup with tag-stripping (`script`, `style`, `nav`, `header`, `footer`, `aside`)

Uses a custom User-Agent string: `Mozilla/5.0 (compatible; LinkedInContentBot/1.0; +research)`.

**Why unused:** `main.py` only instantiates `RSSScanner`. The `WebScraper` was likely built for sources without RSS feeds but was never wired into the orchestrator. The `regulatory`, `academic`, and `vc_blogs` config sections (which might benefit from web scraping) are also never loaded.

### 3.2 Ranking Engine (`src/ranker.py`)

#### Scoring Algorithm (0-100 total)

```
Total Score = Recency (0-30) + Substance (0-25) + Authority (0-20) + Engagement (0-25)
```

**Recency Score (0-30 points) — `_recency_score()`**

Exponential decay function: `30.0 * exp(-0.02 * days_old)`

```
Age         Score
0 days      30.0
1 day       29.4
7 days      25.9
30 days     16.4
90 days      4.9
180 days     0.8 (hard cutoff: returns 0.0 beyond this)
No date      0.0
```

Items without a `published_at` date receive 0 points for recency, effectively disqualifying them since the total score is likely too low to compete. This penalizes the `WebScraper` (which always sets `published_at=None`) and any RSS feeds that omit dates.

**Substance Score (0-25 points) — `_substance_score()`**

| Signal | Points | Logic |
|---|---|---|
| Word count > 50 | +5 | Minimum content threshold |
| Word count > 150 | +5 | Moderate depth |
| Word count > 300 | +5 | Long-form content |
| Numbers/data present | +2 each, max 5 | Regex: `\$[\d,.]+[BMK]?`, `\d+%`, `\d{4,}` |
| Contains quotes | +5 | Regex: text between quotation marks (including unicode variants) |

**Authority Score (0-20 points) — `_authority_score()`**

Direct mapping from `sources.json` priority: `min(priority * 2, 20)`.

| Priority | Score |
|---|---|
| 10 | 20 |
| 9 | 18 |
| 8 | 16 |
| 5 (default) | 10 |
| 1 | 2 |

**Engagement Score (0-25 points) — `_engagement_score()`**

| Signal | Points | Keywords |
|---|---|---|
| High-signal keywords | +3 each, max 10 | `breaking`, `exclusive`, `announced`, `launched`, `partnership`, `acquisition`, `regulation`, `billion`, `million`, `approval`, `ban`, `investigation`, `patent`, `settlement` |
| Topic relevance | +2 each, max 10 | `stablecoin`, `cbdc`, `tokenization`, `embedded finance`, `baas`, `real-time payments`, `cross-border`, `defi`, `regtech`, `open banking`, `generative ai`, `llm`, `artificial intelligence` |
| Contains URL | +5 | Presence of `https?://` in content |

#### Deduplication Strategy

Two-pass dedup in `_deduplicate()`:
1. **Exact URL match** — `set` lookup, O(1) per item
2. **Content similarity** — First 500 chars compared via `SequenceMatcher.quick_ratio()`. Threshold: >0.8 similarity → rejected.

`quick_ratio()` is an optimization that returns an upper bound on the actual ratio. This means it may *overestimate* similarity (incorrectly removing unique items) less often than it *underestimates* it (letting near-duplicates through). For a content pipeline where false negatives (duplicate posts) are worse than false positives (missing a source), this is the wrong direction — `ratio()` would be safer but slower.

#### Selection (No Source Diversity Constraint)

After scoring, items are selected strictly by score descending — top 20 are selected, the rest are rejected. There is no source diversity filter; the user manually picks which articles to generate posts for, so artificial constraints are unnecessary.

### 3.3 Content Generation (`src/content_generator.py`)

#### Claude API Integration

```python
client = anthropic.Anthropic(api_key=api_key)
response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1024,
    system=SYSTEM_PROMPT,        # ~600 tokens of persona/rules
    messages=[{"role": "user", "content": prompt}],  # ~2000 tokens max
)
```

**Model:** `claude-sonnet-4-20250514` (hardcoded default, overridable via constructor).

**Token budget (batch mode — single API call for all 3 posts):**
- System prompt: ~600 tokens (sent once, not 3 times)
- User prompt: ~300 tokens (batch template) + 3 × ~2000 tokens (truncated content) = ~6300 tokens
- Total input: ~6900 tokens (vs ~9300 for 3 individual calls — ~26% reduction)
- Max output: 4096 tokens (3 posts × ~300 tokens = ~900 tokens used)

**Token budget per individual call (fallback mode):**
- System prompt: ~600 tokens
- User prompt: ~500 tokens (template) + ~2000 tokens (truncated content) = ~2500 tokens
- Total input: ~3100 tokens
- Max output: 1024 tokens (a 200-word post uses ~250-300 tokens)

#### Prompt Engineering Approach

The system prompt (`SYSTEM_PROMPT`, 61 lines) is the core quality control mechanism. It operates on five levels:

1. **Voice definition:** Specific > vague, skeptical > breathless, experienced > aspirational
2. **Forbidden phrases:** 10+ banned LinkedIn cliches ("Let that sink in", "Game-changer", etc.)
3. **Forbidden structures:** Rhetorical questions, obvious lists, false equivalencies
4. **Required elements:** Falsifiable claim, concrete data point, uncertainty acknowledgment, "so what"
5. **Self-check instructions:** Red flags to catch before returning

The user prompt (`USER_PROMPT_TEMPLATE`) provides the source article and explicit instructions for post structure: surprising opener → why it matters → uncertainty acknowledgment → closing (question/prediction/relevance).

**Content truncation:** Source content is truncated to 2000 characters before being sent to Claude (`_truncate()` at `content_generator.py:121`). This means Claude sees roughly the first 300-400 words of each article, which may miss key details in longer pieces.

#### Anti-Cringe Post-Processing (`_anti_cringe_filter()`)

Applied *after* Claude's response as a safety net:

1. **Emoji removal:** Keeps max 1 emoji (regex covers major Unicode emoji ranges)
2. **Cringe phrase removal:** Regex-strips "let that sink in", "read that again", "agree?", trailing "thoughts?"
3. **Hashtag cap:** Removes all hashtags beyond the first 3
4. **Whitespace cleanup:** Collapses triple+ newlines to double

This is a defense-in-depth measure. Claude should follow the system prompt's rules, but the filter catches regressions.

#### Error Handling

- **API errors:** `anthropic.APIError` is caught and re-raised in `generate_post()`, but `generate_batch()` catches *all* exceptions per item and continues. A single API failure doesn't abort the batch.
- **No retry logic.** If Claude returns a 429 (rate limit) or 500, the item is skipped. No exponential backoff.
- **No response validation.** The generated post is used as-is (after anti-cringe filter). There's no check for empty responses, refusals, or off-topic content.

### 3.4 Database Layer (`src/database.py`)

#### Schema

```sql
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,          -- Dedup key for upsert
    source_type TEXT NOT NULL,          -- 'rss' or 'twitter'
    category TEXT,                      -- 'fintech', 'stablecoin', 'ai', 'banking', 'payments'
    priority INTEGER DEFAULT 5,         -- 1-10, maps to authority score
    active BOOLEAN DEFAULT 1,           -- Soft delete
    last_scanned TIMESTAMP,             -- Updated after each scan
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scanned_content (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),  -- FK, no CASCADE
    url TEXT UNIQUE NOT NULL,                   -- Dedup key
    title TEXT,
    content TEXT,                               -- Full extracted text (up to 5000 chars)
    author TEXT,
    published_at TIMESTAMP,                     -- From source; NULL if unavailable
    engagement_score REAL,                       -- Written by ranker but never read back
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- Used for "recent content" queries
    selected BOOLEAN DEFAULT 0                  -- Set to 1 when chosen by ranker
);

CREATE TABLE IF NOT EXISTS generated_posts (
    id INTEGER PRIMARY KEY,
    content_id INTEGER REFERENCES scanned_content(id),  -- FK, no CASCADE
    source_summary TEXT,               -- "Source: Author — Title\nLink: URL"
    commentary TEXT,                   -- Same as full_post (redundant)
    full_post TEXT,                    -- The LinkedIn post text
    status TEXT DEFAULT 'draft',       -- 'draft', 'approved', 'posted', 'rejected'
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,             -- Set when status → 'approved'
    posted_at TIMESTAMP                -- Set when status → 'posted'
);
```

```sql
CREATE TABLE IF NOT EXISTS rejected_articles (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL,                 -- ISO date of ranking run
    content_id INTEGER REFERENCES scanned_content(id),
    title TEXT,
    url TEXT,
    source_name TEXT,
    total_score REAL,                       -- Combined score (0-100)
    recency_score REAL,                     -- Component: 0-30
    substance_score REAL,                   -- Component: 0-25
    authority_score REAL,                   -- Component: 0-20
    engagement_score REAL,                  -- Component: 0-25
    rejection_reason TEXT                   -- Why it didn't make the cut
);

CREATE TABLE IF NOT EXISTS ranked_candidates (
    id INTEGER PRIMARY KEY,
    run_date TEXT NOT NULL,                     -- ISO date of ranking run
    content_id INTEGER REFERENCES scanned_content(id),
    title TEXT,
    url TEXT,
    source_name TEXT,
    category TEXT,
    total_score REAL,                           -- Combined score (0-100)
    recency_score REAL,                         -- Component: 0-30
    substance_score REAL,                       -- Component: 0-25
    authority_score REAL,                       -- Component: 0-20
    engagement_score REAL,                      -- Component: 0-25
    status TEXT DEFAULT 'candidate',            -- candidate/generating/generated/error
    generated_post_id INTEGER,                  -- FK to generated_posts.id after generation
    error_message TEXT                          -- Error details if generation failed
);

CREATE TABLE IF NOT EXISTS source_failures (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    source_name TEXT,
    source_url TEXT,
    failure_type TEXT NOT NULL,             -- timeout/http_error/parse_error/zero_results
    error_message TEXT,
    consecutive_zero_count INTEGER DEFAULT 0,  -- For zero_results: how many consecutive runs
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_rejections (
    id INTEGER PRIMARY KEY,
    content_id INTEGER UNIQUE REFERENCES scanned_content(id),  -- Permanent rejection
    rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Indexes:**
```sql
CREATE INDEX idx_content_published ON scanned_content(published_at);
CREATE INDEX idx_content_url ON scanned_content(url);
CREATE INDEX idx_content_scanned_at ON scanned_content(scanned_at);  -- NEW: fixes missing index
CREATE INDEX idx_posts_status ON generated_posts(status);
CREATE INDEX idx_sources_type ON sources(source_type);
CREATE INDEX idx_rejected_run_date ON rejected_articles(run_date);
CREATE INDEX idx_candidates_run_date ON ranked_candidates(run_date);
CREATE INDEX idx_candidates_status ON ranked_candidates(status);
CREATE INDEX idx_source_failures_recorded ON source_failures(recorded_at);
```

#### Connection Management

```python
@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row     # Dict-like access
    conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**WAL mode** is set on every connection open. This is fine for SQLite (it's a persistent setting after the first call) but is unnecessary overhead on subsequent connections.

Each database function opens and closes its own connection. There is no connection pooling. For the current ~100 operations per run, this is fine. At scale, it would be a problem.

#### Notable Patterns

- **`upsert_source()`** uses `INSERT ... ON CONFLICT(url) DO UPDATE` — sources are idempotent on reload.
- **`insert_content()`** catches `IntegrityError` for URL uniqueness — duplicates are silently skipped.
- **`get_candidate_pool(days=5)`** returns all articles scanned in the last 5 days, excluding those with generated posts or user rejections. This creates a rolling pool where good articles persist across multiple scan runs. The older `get_recent_content(hours=48)` is retained but no longer used by the ranking pipeline.
- **`commentary` and `full_post` are always identical** in `content_generator.py:148-149`. The `commentary` field is redundant.

### 3.5 Dashboard (`dashboard/app.py`)

#### Flask Application Structure

```
dashboard/
├── app.py                 # 9 routes, DB init, generation API, dev server
├── templates/
│   ├── base.html          # Nav bar (Drafts, Candidates, History, Rejected, Sources), CSS/JS includes
│   ├── index.html         # Draft review cards
│   ├── candidates.html    # Ranked article selection + on-demand generation
│   ├── history.html       # Post history with status filters
│   ├── rejected.html      # Top 20 rejected articles with score breakdowns
│   └── source_health.html # Source failure log (hard + soft failures)
└── static/
    ├── style.css          # Responsive, LinkedIn-blue theme
    └── script.js          # Clipboard, status update, candidate selection, toast
```

#### Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Render up to 10 draft posts for review |
| `/candidates` | GET | Ranked candidate articles for manual selection and generation |
| `/history` | GET | Render post history, filterable by status via `?status=` query param |
| `/rejected` | GET | Rejected articles: user-rejected candidates + score-rejected articles with breakdowns |
| `/source-health` | GET | Recent source failures (hard errors + soft zero-result tracking) |
| `/api/candidates` | GET | JSON API returning ranked candidates |
| `/api/generate` | POST | Generate a post for one candidate (body: `{"candidate_id": int}`) |
| `/api/drafts` | GET | JSON API returning draft posts (unused by current frontend) |
| `/api/candidates/<id>/reject` | POST | Permanently reject a candidate from future ranking pools |
| `/api/posts/<id>/status` | POST | Update post status (body: `{"status": "approved"}`) |

#### Frontend Architecture

**Vanilla JS, no framework.** The frontend is server-rendered Jinja2 templates with minimal client-side behavior:

- **Copy to clipboard:** Uses `navigator.clipboard.writeText()` with a `document.execCommand("copy")` fallback. HTML entities in `data-text` attributes are manually unescaped.
- **Status updates:** `fetch()` calls to `/api/posts/<id>/status`. On success, the card fades out (opacity transition) and is removed from the DOM. When all cards are removed, an empty state is injected.
- **Toast notifications:** Dynamically created `<div class="toast">` appended to body, shown/hidden via CSS class toggle.

**No polling or real-time updates.** If new drafts are generated while the dashboard is open, a page refresh is required.

#### User Workflow

1. Pipeline runs (daily cron or manual) → scans sources, ranks top 20 candidates
2. Open dashboard → navigate to Candidates page
3. Review ranked articles with scores → select articles to generate posts for
4. Click "Generate N Posts" → posts are generated sequentially, UI updates in real-time
5. Navigate to Drafts (`/`) → see generated post drafts
6. Read each post, click "Copy to Clipboard" → paste into LinkedIn
7. Click "Approve" (changes status, removes card) or "Reject" (same)
8. Navigate to History to see all past posts with status filters

### 3.6 Orchestration (`src/main.py`)

#### Daily Execution Logic (`run_daily()`)

```python
def run_daily():
    init_db()                          # 1. Ensure tables exist
    load_sources_from_config()         # 2. Sync sources.json → sources table
    scan_all_sources()                 # 3. Scan all active RSS sources
    rank_candidates(top_n=20)          # 4. Rank from 5-day candidate pool
```

Ranking always runs regardless of whether new items were found — the 5-day candidate pool may contain articles from previous days that are still viable. Post generation is triggered manually via the dashboard (`POST /api/generate`), not during the pipeline run.

**Dependency ordering:** Strictly sequential. Each step depends on the previous one.

**Idempotency considerations:**
- `load_sources_from_config()` is idempotent (upserts).
- `scan_all_sources()` is idempotent for content already in DB (URL uniqueness check). However, running twice in quick succession would double the `scanned_at` window, potentially surfacing the same items for ranking again.
- `rank_candidates()` is idempotent — `insert_ranked_candidates()` deletes all previous candidates before inserting fresh ones. Any previously generated posts remain in `generated_posts` unaffected.

**Error handling:** No top-level try/except in `run_daily()`. If `init_db()` fails (disk full, permissions), the entire run crashes. The GitHub Actions workflow doesn't have explicit failure notification — it relies on GitHub's default email for failed workflow runs.

#### Config Loading

`sources.json` is loaded once and cached in a module-level `_config_cache`. This prevents re-reading the file but means the cache persists for the process lifetime. Not an issue for the current batch-run design.

**Bug:** `load_sources_from_config()` only processes 4 of 7 config sections:

```python
for section in ("newsletters", "blogs", "news"):  # ← Only 3 + twitter_accounts
```

The `regulatory` (10 sources), `academic` (10 sources), and `vc_blogs` (12 sources) sections are silently ignored. These 32 sources are defined in the config but never loaded into the database.

---

## 4. EXTERNAL INTEGRATIONS

### 4.1 Anthropic Claude API

| Parameter | Value |
|---|---|
| Model | `claude-sonnet-4-20250514` |
| Max output tokens | 1024 |
| Calls per run | 1 (batch mode, all 3 articles in single call; falls back to 3 individual calls on failure) |
| Authentication | `ANTHROPIC_API_KEY` env var |
| Client library | `anthropic>=0.39.0` |
| Error handling | `APIError` caught in batch loop, item skipped |
| Rate limiting | None (relies on Anthropic's server-side limits) |
| Retry logic | None |

**Cost estimate per run (batch mode):**
- Input: ~6900 tokens × 1 call = ~6900 tokens → ~$0.021 (at $3/M input tokens)
- Output: ~900 tokens × 1 call = ~900 tokens → ~$0.014 (at $15/M output tokens)
- **Total: ~$0.035/run, ~$1.05/month for daily runs** (~17% savings vs individual calls)

### 4.2 GitHub Actions

**Workflow:** `.github/workflows/daily_scan.yml`

- **Trigger:** `cron: '0 8 * * *'` (8:00 AM UTC daily) + manual `workflow_dispatch`
- **Runner:** `ubuntu-latest`
- **Python:** 3.11 with pip caching
- **Persistence:** SQLite DB uploaded/downloaded as GitHub Actions artifact
- **Artifact retention:** 90 days (`retention-days: 90`)
- **Secrets:** `ANTHROPIC_API_KEY` stored in repo secrets

**Artifact storage concern:** If no workflow runs for 90 days, the artifact expires and all historical data is lost. The next run starts with a fresh database. The `continue-on-error: true` on the download step handles the first-run case but also silently handles this data loss scenario.

### 4.3 Render.com Deployment

**Config:** `render.yaml`

```yaml
services:
  - type: web
    name: linkedin-content-dashboard
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: python dashboard/app.py      # ← Dev server, not gunicorn
    envVars:
      - key: DATABASE_PATH
        value: /data/content_history.db
      - key: PORT
        value: "10000"
      - key: ANTHROPIC_API_KEY
        sync: false                             # ← Manual setup required
    disk:
      name: content-data
      mountPath: /data
      sizeGB: 1
    plan: free
    autoDeploy: true
```

**Free tier limitations:**
- Service spins down after 15 minutes of inactivity (cold start on next request)
- 750 hours/month of runtime
- No custom domains on free tier
- Build cache may be evicted

### 4.4 Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes (for generation) | None | Claude API authentication |
| `DATABASE_PATH` | No | `data/content_history.db` (relative) | SQLite file location |
| `PORT` | No | `5001` | Dashboard web server port |
| `FLASK_DEBUG` | No | `"0"` | Enable Flask debug mode |

---

## 5. CONFIGURATION MANAGEMENT

### 5.1 `sources.json` Structure

Top-level keys map to source categories:

```json
{
  "twitter_accounts": [...],   // 15 entries — DISABLED (not scanned)
  "newsletters": [...],        // 28 entries — Active
  "blogs": [...],              // 8 entries  — Active
  "news": [...],               // 27 entries — Active (some paywalled)
  "regulatory": [...],         // 13 entries — Active (newly loaded)
  "academic": [...],           // 10 entries — Active (newly loaded)
  "vc_blogs": [...]            // 13 entries — Active (newly loaded)
}
```

**Actually scanned:** ~90 RSS sources (all sections except twitter_accounts, minus 9 disabled sources).
**Disabled RSS sources:** 9 entries with `"enabled": false` (dead domains, discontinued feeds, non-RSS pages).
**Configured but dead:** 15 Twitter accounts (scanner disabled).

Each source entry:
```json
{
  "name": "Human-readable name",
  "url": "https://feed.url/rss",     // RSS feed URL
  "handle": "twitter_handle",         // Twitter accounts only
  "category": "fintech",              // One of: fintech, stablecoin, ai, banking, payments
  "priority": 8,                      // 1-10, maps to authority score (priority × 2)
  "enabled": false,                   // Optional — set to false to skip loading (dead/broken sources)
  "_note": "Optional human note"      // Ignored by code
}
```

Sources with `"enabled": false` are skipped by `load_sources_from_config()` and never inserted into the database. Currently 9 sources are disabled (dead domains, discontinued feeds, non-RSS pages).

### 5.2 Priority Score Impact

Priority directly controls the Authority Score component (0-20 points out of 100 total):

| Priority | Authority Points | Practical Effect |
|---|---|---|
| 9-10 | 18-20 | Strongly preferred — can overcome 1-2 day age disadvantage |
| 7-8 | 14-16 | Favored if content is reasonably fresh |
| 5-6 | 10-12 | Neutral — competes on content quality alone |
| 1-4 | 2-8 | Disadvantaged — needs strong recency and substance to rank |

High-priority sources in the current config: Jeremy Allaire (Circle CEO, 9), Fintech Brainfood (9), Money Stuff (9, but paywalled).

### 5.3 Adding/Removing Sources

**To add a source:**
1. Add entry to appropriate section in `sources.json`
2. Ensure URL is a valid RSS/Atom feed
3. Choose category from: `fintech`, `stablecoin`, `ai`, `banking`, `payments`
4. Set priority 1-10 based on source authority
5. Commit and push — next `run_daily()` will upsert it

**To remove a source:**
1. Remove entry from `sources.json`
2. The source remains in the DB (with `active=1`) but won't be upserted with new data
3. To fully deactivate, manually set `active=0` in the DB

**Gotcha:** Removing a source from the JSON doesn't deactivate it in the DB. It just stops updating its metadata. If you need it gone, you must also clean the DB.

### 5.4 Category Taxonomy

Five categories, used for:
1. Dashboard badge coloring (CSS class `badge-{category}`)
2. Stored in DB for filtering potential (currently unused in queries)
3. No category-based filtering in ranking or generation

Categories are freeform text in the config — there's no validation that categories match the 5 expected values.

---

## 6. DATA PERSISTENCE

### 6.1 SQLite Schema

Full `CREATE TABLE` statements are in Section 3.4. Key characteristics:

- **WAL journal mode:** Enables concurrent reads during writes. Set per-connection via PRAGMA.
- **Foreign keys enabled:** `sources.id` ← `scanned_content.source_id` ← `generated_posts.content_id`. No `ON DELETE CASCADE` — deleting a source orphans its content.
- **No `CHECK` constraints** on `status` — any string value is accepted by the DB.

### 6.2 Disk Storage

**GitHub Actions:** SQLite file stored as a build artifact. Size at current scale: <1MB. At 200 items/day for 90 days (retention limit), expect ~50-100MB.

**Render.com:** 1GB persistent disk mounted at `/data`. At current growth rates, this is sufficient for years of data. SQLite with WAL mode creates `-wal` and `-shm` companion files that share the same directory.

### 6.3 Deployment/Restart Behavior

| Scenario | DB Impact |
|---|---|
| GitHub Actions run | Downloads artifact → runs pipeline → uploads artifact. If download fails (first run or expired), starts fresh. |
| Render deploy | Persistent disk survives deploys. DB is retained. |
| Render restart (free tier spin-down) | Disk persists. No data loss. |
| Render disk deletion | All data lost. No backup. |

### 6.4 Data Retention

There is no automated cleanup. Tables grow indefinitely. Projected growth:

| Table | Rows/Day | Rows/Year | Estimated Size/Year |
|---|---|---|---|
| `sources` | 0 (static) | ~70 | Negligible |
| `scanned_content` | ~50-200 | ~36,000-73,000 | ~50-150MB (with content text) |
| `generated_posts` | ~3 | ~1,100 | ~1-2MB |

The `scanned_content.content` column stores up to 5000 chars per row, making it the primary storage driver.

---

## 7. COST & PERFORMANCE

### 7.1 Claude API Cost

**Current (manual selection, individual calls):**

| Component | Tokens/Call | Cost/Call | Notes |
|---|---|---|---|
| Input (system + 1 article) | ~3,100 | ~$0.009 | Per article selected |
| Output (1 post) | ~300 | ~$0.005 | Per article selected |
| **Total per post** | | **~$0.014** | |

Cost is now per-post since generation is on demand via individual API calls. At 3 posts/day: ~$0.04/day, ~$1.26/month. At 10 posts/day: ~$0.14/day, ~$4.20/month.

**Scanning cost:** $0 — scanning and ranking use no API calls. Only generation (triggered manually) incurs Claude API cost.

### 7.2 Scanning Runtime

Bottleneck is sequential RSS fetching with 1-second rate limiting:

```
~90 sources × (HTTP fetch time + 1s rate limit) ≈ 90 × ~2-3s = ~180-270 seconds
```

Some sources will be slow or timeout (default `feedparser` timeout). A single slow source blocks the entire pipeline. Sources with SSL certificate failures may take an extra retry (~doubled time for those sources).

**Optimization opportunity:** Parallel scanning with `asyncio` or `concurrent.futures.ThreadPoolExecutor` could reduce this to ~10-20 seconds but would need careful rate limiting per-domain.

### 7.3 Database Query Performance

All critical queries are indexed:
- `get_candidate_pool()` → uses `scanned_at` (covered by `idx_content_scanned_at`)
- `get_drafts()` → `idx_posts_status` covers `WHERE status = 'draft'`
- `insert_content()` → `idx_content_url` makes UNIQUE constraint check fast

**Missing index:** `scanned_at` is the column used in `get_recent_content()` (`WHERE sc.scanned_at >= datetime('now', ...)`), but the index is on `published_at`. This query will do a full table scan on `scanned_content`. At 70K rows/year, this becomes noticeable.

### 7.4 Render.com Resource Usage

- **Memory:** Flask + SQLite is lightweight. <50MB expected.
- **CPU:** Negligible for serving dashboard pages.
- **Disk:** 1GB allocated, <1MB used currently. Years of headroom.
- **Bandwidth:** Minimal — single user, occasional page loads.

---

## 8. TESTING & OBSERVABILITY

### 8.1 Test Coverage

**None.** There are no test files in the repository. No `tests/` directory, no `pytest.ini`, no test configuration.

Components that most need tests:
1. `ranker.py` — scoring algorithm with specific expected behaviors
2. `_anti_cringe_filter()` — regex-based text processing
3. `database.py` — CRUD operations, especially edge cases (duplicate handling, missing FKs)
4. `RSSScanner._parse_date()` — date parsing with multiple fallback paths

### 8.2 Logging Strategy

All modules use Python's `logging` module with a shared format configured in `main.py`:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
```

**What's logged:**
- Source loading count
- Per-source scan start and item count
- Ranking results (item count, scores of selected)
- Post generation start per item
- Post insertion confirmation
- Errors at every stage (scanner failures, API errors, missing keys)

**Where it goes:** `stdout`. In GitHub Actions, this appears in the workflow run logs. On Render, it appears in the service logs.

**What's missing:**
- No structured logging (JSON) for machine parsing
- No request logging on the dashboard (Flask doesn't log by default in production)
- No metrics (scan duration, success rate, API latency)
- No alerting beyond GitHub Actions' default email on failure

### 8.3 Debugging Failed Runs

1. **GitHub Actions:** Navigate to Actions tab → select failed run → expand step logs
2. **Common failures:**
   - `ANTHROPIC_API_KEY not set` → check repo secrets
   - `Feed parse error for <url>` → source feed is down or changed format
   - `Claude API error` → rate limit, invalid key, or API outage
   - `No recent content found` → all feeds returned old content or failed
3. **Local reproduction:** `python src/main.py` with `.env` file containing API key
4. **Database inspection:** `sqlite3 data/content_history.db` → query tables directly

### 8.4 Monitoring Recommendations

- Add structured JSON logging for production observability
- Track per-source success/failure rates to identify degraded feeds
- Monitor `scanned_content` growth to predict disk pressure
- Set up Render health check endpoint (currently none)
- Add GitHub Actions workflow status badge to README

---

## 9. DEPLOYMENT & CI/CD

### 9.1 GitHub Actions Workflow

```
Trigger (8AM UTC cron or manual) ──▶ Checkout code
    ──▶ Setup Python 3.11 (with pip cache)
    ──▶ pip install -r requirements.txt
    ──▶ Download artifact "content-database" (continue-on-error)
    ──▶ python src/main.py (with ANTHROPIC_API_KEY from secrets)
    ──▶ Upload artifact "content-database" (90-day retention, overwrite)
```

**Critical note on `overwrite: true`:** Each run overwrites the previous artifact. There's no versioned history of the database. If a run corrupts the DB, the next upload overwrites the good version.

### 9.2 Render.com Deployment

- **Trigger:** `autoDeploy: true` deploys on every push to the connected branch
- **Build:** `pip install -r requirements.txt`
- **Start:** `python dashboard/app.py` (Flask dev server)
- **Persistent disk:** Mounted at `/data`, survives deploys and restarts

**No gunicorn.** Despite being in `requirements.txt`, gunicorn is not used. The start command should be:
```
gunicorn dashboard.app:app --bind 0.0.0.0:$PORT
```

### 9.3 Environment Parity

| Aspect | Local | GitHub Actions | Render |
|---|---|---|---|
| Python version | User's install | 3.11 | Render's default |
| DB location | `data/content_history.db` | `data/content_history.db` | `/data/content_history.db` |
| API key source | `.env` file | Repo secret | Render env var |
| Runs scanner | Yes | Yes | No (dashboard only) |
| Runs dashboard | Yes (port 5001) | No | Yes (port 10000) |

**Gap:** There's no way to run the scanner on Render. The scanner runs in GitHub Actions and stores results in an artifact. The dashboard on Render has its own empty DB unless manually seeded.

### 9.4 Rollback Procedure

- **Code rollback:** `git revert` + push → Render auto-deploys previous code
- **Database rollback:** No mechanism. Artifact overwrite is destructive. Manual DB backup would need to be implemented.
- **Full recovery from scratch:** Delete DB, run `main.py` once. Loses all historical posts and approval status, but regenerates fresh content.

---

## 10. ITERATION ROADMAP CONSIDERATIONS

### 10.1 Adding New Content Sources

**RSS sources:** Add to `sources.json` under the appropriate section. All 7 sections are processed by `load_sources_from_config()`. Set `"enabled": false` to disable a source without removing it from the config.

**New scanner types** (e.g., Reddit, Hacker News, LinkedIn itself):
1. Create `src/scanners/new_scanner.py` extending `BaseScanner`
2. Implement `scan()` returning `list[ScannedItem]`
3. Add a new `source_type` string (e.g., `"reddit"`)
4. Instantiate in `main.py.scan_all_sources()` and filter sources by type
5. Add entries to `sources.json` under a new section
6. Update `load_sources_from_config()` to process the new section

### 10.2 Scalability Constraints

| Constraint | Current Limit | Breaking Point | Mitigation |
|---|---|---|---|
| SQLite concurrent writes | 1 writer at a time | Multiple processes writing simultaneously | Migrate to Postgres |
| Sequential scanning | O(n) with n sources | >200 sources → >10 min runtime | Parallel scanning with thread pool |
| GitHub Actions runtime | 6 hours max per job | Not a near-term concern | Split into multiple jobs |
| Artifact storage | 10GB per repo | Years of headroom at current scale | Migrate to external storage |
| Render free tier | 750 hours/month, cold starts | Production traffic patterns | Upgrade to paid tier |

### 10.3 ML/AI Enhancement Points

1. **Ranking model:** Replace heuristic scoring with a learned model trained on user approval/rejection signals. The `generated_posts.status` field already captures this label.
2. **Content summarization:** Pre-summarize long articles before sending to Claude to improve post quality for long-form sources.
3. **Personalization:** Learn which topics and sources the user consistently approves to bias future ranking.
4. **A/B testing prompts:** Generate 2 variants per article, let user pick the better one, use preferences to refine system prompt.
5. **Engagement prediction:** If LinkedIn posting is eventually automated, feed back engagement metrics (likes, comments) to improve ranking.

### 10.4 Dashboard Feature Extensibility

The current Flask + vanilla JS architecture supports:
- Adding new routes and templates easily
- Simple API additions

It does **not** easily support:
- Real-time updates (would need WebSockets or SSE)
- Complex state management (would need a JS framework)
- Rich text editing of posts before posting
- Multi-page workflows (no client-side routing)

For significant dashboard evolution, consider migrating to a React/Next.js frontend with the Flask backend as a pure API.

### 10.5 Multi-User Considerations

The system is **fundamentally single-user:**
- No authentication on the dashboard
- No user concept in the database
- No per-user source configuration
- No access control on API endpoints
- Single `sources.json` with one person's preferences

Adding multi-user support would require:
- Authentication (OAuth, session management)
- User table with FK relationships to sources and posts
- Per-user source lists and priorities
- Per-user Claude API key or billing
- Tenant isolation in queries

This is a significant architectural change, not a feature addition.

---

## 11. CODE QUALITY & MAINTAINABILITY

### 11.1 Dependency Versions

| Package | Pinned Version | Latest Concern |
|---|---|---|
| `anthropic>=0.39.0` | Floor only | Major version bumps may break `messages.create()` API |
| `flask>=3.0.0` | Floor only | Stable, low risk |
| `feedparser>=6.0.0` | Floor only | Stable, rarely updated |
| `requests>=2.31.0` | Floor only | Stable |
| `beautifulsoup4>=4.12.0` | Floor only | Stable |
| `trafilatura>=1.8.0` | Floor only | Active development, may change extraction behavior |
| `gunicorn>=21.2.0` | Floor only | Unused in production |
| `python-dotenv>=1.0.0` | Floor only | Stable |
| `python-dateutil>=2.8.0` | Floor only | Stable |

**No `requirements.lock` or pinned versions.** Builds are not reproducible. A transitive dependency update could break the pipeline silently. Recommend adding a `requirements.lock` generated by `pip freeze`.

### 11.2 Code Organization Patterns

**Strengths:**
- Clean separation: scanners, ranker, generator, database, dashboard are distinct modules
- Scanner abstraction (`BaseScanner`) enables adding new source types
- Database functions are pure functions (no class state), easy to test
- Configuration is externalized in `sources.json`

**Weaknesses:**
- `main.py` has business logic (source loading, orchestration) that should be in dedicated modules
- `content_generator.py` mixes prompt templates, API calls, and text processing in one file
- No dependency injection — modules import database functions directly, making testing harder
- `sys.path.insert(0, ...)` hacks in `main.py` and `app.py` for imports

### 11.3 Where Abstractions Are Missing

1. **No Source model.** Sources are passed as raw `dict` objects everywhere. A `Source` dataclass would add type safety.
2. **No Post model.** Generated posts are also raw dicts. Status transitions are string comparisons.
3. **No pipeline abstraction.** `run_daily()` is procedural. A pipeline pattern (with steps, rollback, logging hooks) would improve reliability.
4. **No configuration validation.** `sources.json` is parsed but never validated against a schema. Missing fields cause runtime errors deep in the scanner.

### 11.4 Technical Debt Hotspots

| Location | Issue | Severity | Fix Effort |
|---|---|---|---|
| ~~`main.py:70-79`~~ | ~~Only 4 of 7 config sections loaded~~ | ~~High~~ **FIXED** | — |
| `main.py:27-28` | Dead Twitter import + 15 unused config entries | Medium — confusing to new developers | Low — remove or document clearly |
| `content_generator.py:148-149` | `commentary` duplicates `full_post` | Low — wasted storage | Low — remove field or differentiate |
| `ranker.py:189` | `quick_ratio()` vs `ratio()` | Medium — may allow near-duplicate posts | Low — switch to `ratio()` |
| `render.yaml:6` | Flask dev server in production | High — no worker management, no graceful shutdown | Low — change to gunicorn |
| ~~`database.py` (general)~~ | ~~No `scanned_at` index~~ | ~~Medium~~ **FIXED** | — |
| `app.py:50-54` | No post existence check on status update | Low — silent no-op | Low — add existence check |
| No test suite | Entire codebase untested | High — any change is risky | Medium — add pytest + fixtures |
| No DB sync | GH Actions and Render use separate DBs | Critical — dashboard shows no data | Medium — needs architectural decision |

---

*This document is kept up to date with architectural changes. See changelog below.*

---

## 12. CHANGELOG

### 2026-02-03 — Enhancement Batch (post `34ee7c8`)

**1. Dashboard: Top 20 Rejected Articles** (`/rejected` route)
- Added `rejected_articles` table storing top 20 non-selected articles per ranking run
- Ranker now computes and exposes per-component score breakdowns (recency/substance/authority/engagement)
- Each rejected article includes a human-readable rejection reason ("Outside top 3", "Beat by higher-scoring article from same source")
- New dashboard page at `/rejected` with score chips and clickable source links

**2. Source Failure Logging**
- Added `source_failures` table tracking both hard failures (timeout/parse_error/http_error) and soft failures (zero_results)
- `scan_safe()` now returns structured failure info (type + message) instead of silently returning `[]`
- Consecutive zero-result count tracked per source; warning logged after 3+ consecutive zeros
- Hard failures also appended to `data/source_failures.jsonl` for grep-friendly review
- New dashboard page at `/source-health` showing failure history with color-coded severity

**3. API Call Optimization (Batch Generation)**
- `generate_batch()` now sends all 3 articles in a single Claude API call using a combined prompt
- Posts separated by `---POST_SEPARATOR---` delimiter for reliable parsing
- Automatic fallback to individual calls if batch parsing fails (wrong number of posts returned)
- ~26% reduction in input tokens (system prompt sent once instead of 3 times)
- ~17% reduction in per-run cost ($0.035 vs $0.04)

**4. New Sources Added** (14 new, 3 updated)
- **New newsletters:** Pragmatic Engineer, Fintech GTM (Alex Johnson), Bank Autonomy, Working Theorys (Lawrence Lundy)
- **New blogs:** Warp Dev Blog, Stripe Engineering, Matthew Ball, 11:FS Fintech Insider
- **New news:** Hacker News Show HN
- **New regulatory:** FDIC Federal Register, OCC Federal Register, Federal Reserve Press Releases
- **New vc_blogs:** a16z Fintech (Angela Strange)
- **Updated existing:** Net Interest (added Marc Rubinstein), Fintech Business Weekly (added Jason Mikula, priority 7→8), Bits About Money (added Patrick McKenzie, priority 8→9)
- **Skipped:** `getrevue.co` (Revue shut down in 2023; Simon Taylor already tracked via Twitter entry)

**5. Bug Fixes**
- **Fixed:** `load_sources_from_config()` now processes all 7 config sections (was only processing 4 of 7, silently ignoring 32 sources)
- **Fixed:** Added missing `scanned_at` index on `scanned_content` table (core ranking query was doing full table scan)
- **Fixed:** `RSSScanner` now raises on feed parse errors instead of silently returning `[]`, enabling proper failure classification

**6. Dashboard Navigation**
- Added "Rejected" and "Sources" links to navbar for accessing new pages

### 2026-02-04 — Manual Article Selection for Post Generation

**1. Pipeline Split: Scan+Rank Only**
- `select_and_generate()` replaced by `rank_candidates(top_n=20)` — pipeline no longer auto-generates posts
- Ranker now selects top 20 (up from 3) candidate articles with source diversity constraint
- Removed `mark_content_selected()` and `ContentGenerator` calls from pipeline
- Post generation moved to on-demand dashboard API

**2. Ranked Candidates Table**
- New `ranked_candidates` table stores top 20 scored articles per scan run
- Schema: run_date, content_id, title, url, source_name, category, score breakdown, status (candidate/generating/generated/error), generated_post_id, error_message
- `insert_ranked_candidates()` clears all previous candidates on each run
- Stale `generating` statuses auto-reset to `candidate` on page load

**3. Dashboard: Candidate Selection Page** (`/candidates` route)
- New page showing ranked articles with scores, checkboxes for selection
- Bulk actions: Select All / Deselect All, Generate N Posts button
- Per-article: score breakdown chips, status badges, generated post preview with copy button
- Generation runs sequentially per article with real-time UI updates

**4. Generation API** (`POST /api/generate`)
- Accepts `{"candidate_id": int}`, generates a single post via `ContentGenerator.generate_post()`
- Status transitions: candidate → generating → generated (or error)
- On success: inserts into `generated_posts` table, links via `generated_post_id`
- On error: stores error message, allows retry (resets status)
- Returns 409 if candidate already generated

**5. Navigation Update**
- Added "Candidates" link between "Drafts" and "History" in navbar

### 2026-02-04 — Multi-Day Candidate Pool and Ranking Overhaul

**1. Rolling 5-Day Candidate Pool**
- New `get_candidate_pool(days=5)` replaces `get_recent_content(hours=48)` for ranking
- Pool includes all articles scanned in the last 5 calendar days
- Articles with generated posts (any status) are excluded from the pool
- Articles explicitly rejected by the user are excluded from the pool
- Articles naturally age out after 5 days if not acted upon
- Ranking now always runs, even if no new articles were found in the current scan (the pool may contain articles from previous days)

**2. Source Diversity Constraint Removed**
- `rank_content()` no longer enforces one-item-per-source
- Top 20 articles are selected strictly by score descending
- Default `top_n` changed from 3 to 20
- Users manually choose which articles to generate, making artificial diversity constraints unnecessary

**3. User Candidate Rejection**
- New `candidate_rejections` table permanently tracks rejected article content_ids
- `POST /api/candidates/<id>/reject` endpoint for rejecting candidates from the dashboard
- Rejected articles are excluded from `get_candidate_pool()` on all future runs
- Reject button added to each candidate card in the candidates view
- Rejected candidates visible on the `/rejected` page under "User-Rejected Candidates" section

**4. Rejected Articles View Enhanced**
- `/rejected` page now shows two sections: user-rejected candidates and score-rejected articles
- Limit increased from 20 to 50 for both sections

**5. Duplicate Logging Fixed**
- Removed duplicate "Ranked N candidate articles for manual selection" log line from `run_daily()` (was already logged inside `rank_candidates()`)

**6. Article Lifecycle States**
```
scanned (in scanned_content table)
    ├── in candidate pool (scanned in last 5 days, no generated post, not rejected)
    │   ├── ranked candidate (in top 20 by score, stored in ranked_candidates)
    │   │   ├── generated (user selected + post created)
    │   │   └── rejected by user (permanently excluded from pool)
    │   └── below top 20 (in rejected_articles table, still in pool for next ranking)
    ├── has generated post (excluded from pool)
    ├── rejected by user (excluded from pool)
    └── aged out (older than 5 days, no longer in pool)
```

### 2026-02-04 — RSS Feed Reliability Fixes

**1. Browser User-Agent for RSS Scanner** (`rss_scanner.py`)
- `feedparser.parse()` now sends a Chrome-like `User-Agent` header on every request
- Root cause: ~25+ sources were returning HTML error pages or bot-detection challenges to feedparser's default user agent, causing XML parse failures across government sites (OCC, FDIC, NY Fed), news outlets (Axios, American Banker, Bloomberg), tech blogs (a16z, Stanford HAI, CB Insights), and others
- This single fix resolves the majority of the 43 feed failures logged on 2026-02-04

**2. SSL Certificate Fallback** (`rss_scanner.py`)
- New `_fetch_feed()` method detects `CERTIFICATE_VERIFY_FAILED` errors and retries with an unverified SSL context
- Fixes ECB Working Papers (self-signed certificate chain) and 11:FS Fintech Insider (hostname mismatch)

**3. Feed URL Corrections** (`sources.json`)
- **Benedict's Newsletter:** `ben-evans.com/benedictevans/feed` → `ben-evans.com/benedictevans?format=rss` (Squarespace format parameter)
- **OpenAI Blog:** `openai.com/blog/rss/` → `openai.com/blog/rss.xml` (site redesign broke old path)
- **Anthropic Blog:** `anthropic.com/news/rss.xml` → community-maintained GitHub RSS feed (Anthropic has no official RSS)
- **Sam Altman:** `blog.samaltman.com/feed` → `blog.samaltman.com/posts.atom` (Posthaven Atom feed)
- **The Neuron:** `theneurondaily.com/feed` → `rss.beehiiv.com/feeds/N4eCstxvgX.xml` (Beehiiv platform feed)
- **Matthew Ball:** `matthewball.co/rss` → `matthewball.co/?format=rss` (Squarespace format parameter)
- **ECB Working Papers:** `ecb.europa.eu/rss/wppub.html` → `ecb.europa.eu/rss/wppub.xml` (wrong file extension)

**4. Disabled Broken Sources** (`sources.json`)
- Added `"enabled": false` flag support in `load_sources_from_config()` — disabled sources are skipped during loading
- **Money Stuff:** Bloomberg account page, not an RSS feed (no public RSS exists)
- **The Batch:** deeplearning.ai webpage, no public RSS feed exists
- **Bank Autonomy:** Domain no longer resolves (DNS failure)
- **Elad Blog:** Domain no longer resolves (DNS failure)
- **AltFi:** Returns HTML, no RSS feed available
- **Reuters AI:** Reuters discontinued public RSS feeds (returns HTML)
- **Digital Transactions:** Returns HTML, no RSS feed available
- **IMF FinTech Notes:** URL is an HTML page, not a valid RSS endpoint
- **FinCEN:** Server refuses all connections

**5. Failure Log Cleared**
- Cleared `data/source_failures.jsonl` for clean baseline after fixes
