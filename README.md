# LinkedIn Content Automation

Automated system that scans fintech/stablecoin/AI/banking sources daily, identifies engagement-worthy content, and generates professional LinkedIn post drafts.

## How It Works

1. **Scan** — RSS feeds, Twitter (via Nitter), and web pages from 50 curated sources
2. **Rank** — Score content by recency, substance, source authority, and engagement signals
3. **Generate** — Claude API creates 3 LinkedIn post drafts with professional, analytical tone
4. **Review** — Flask dashboard to review, approve, copy, and track posts

## Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd linkedin-content-automation
pip install -r requirements.txt
```

### 2. Configure API key

```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

Get a Claude API key at [console.anthropic.com](https://console.anthropic.com) → Settings → API Keys. Cost is approximately $0.01-0.03 per post generation.

### 3. Run locally

```bash
# Run the daily scan + generation
python src/main.py

# Start the dashboard
python dashboard/app.py
# Open http://localhost:5000
```

## Deployment

### GitHub Actions (daily scanning)

1. Go to your repo → Settings → Secrets and variables → Actions
2. Add secret: `ANTHROPIC_API_KEY`
3. The workflow runs daily at 8AM UTC, or trigger manually from the Actions tab

### Render.com (dashboard hosting)

1. Connect your GitHub repository on [render.com](https://render.com)
2. It will auto-detect `render.yaml`
3. Set `ANTHROPIC_API_KEY` in the Render dashboard environment variables
4. Deploy — persistent disk stores the SQLite database

## Managing Sources

Edit `config/sources.json` to add, remove, or reprioritize sources. Each source has:

- `name` — Display name
- `url` — RSS feed URL or Twitter handle
- `category` — fintech, stablecoin, ai, banking, payments
- `priority` — 1-10 (higher = more likely to be selected)

## Project Structure

```
src/
  main.py              — Daily orchestrator
  database.py          — SQLite operations
  ranker.py            — Content scoring & selection
  content_generator.py — Claude API integration
  scanners/            — RSS, Twitter, web scraping
dashboard/
  app.py               — Flask web app
  templates/           — HTML templates
  static/              — CSS & JavaScript
config/
  sources.json         — 50 curated sources
```
