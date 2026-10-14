# AI Daily Brief

AI Daily Brief is a fully automated daily AI news and research brief.

It collects AI-related research papers and news from multiple sources, ranks the most important items, summarizes them using NVIDIA NIM, and publishes the final brief to a Notion database.

GitHub Actions runs the publishing pipeline twice daily. An optional Cloudflare Worker deployment provides health, status, and source-debug endpoints, but does not own the production schedule.

## What This Project Does

Every day, this project publishes:

- Top 5 latest AI research papers from arXiv
- Top 10 AI news/model updates

The final output is written into a Notion database with ranks like:

- `P1`, `P2`, `P3`, `P4`, `P5` for papers
- `N1`, `N2`, ..., `N10` for news

## Data Sources

The Worker collects data from:

- arXiv
- Hacker News
- Hugging Face
- Techmeme

Each source is normalized into a common item format before ranking and publishing.

## How It Works

The full pipeline looks like this:

```text
GitHub Actions Schedule
        |
        v
Python CLI
        |
        v
Collect from arXiv, Hacker News, Hugging Face, Techmeme
        |
        v
Filter AI-related content
        |
        v
Deduplicate repeated links
        |
        v
Rank papers and news
        |
        v
Use NVIDIA NIM for news ranking and summaries
        |
        v
Check Notion for existing Date + Rank rows
        |
        v
Publish missing rows to Notion
```

## Features

- Runs automatically every day in the cloud
- Works even when the laptop is off
- Uses GitHub Actions for free scheduled execution
- Collects papers from arXiv
- Collects AI stories from Hacker News
- Collects trending model updates from Hugging Face
- Collects AI-related stories from Techmeme's RSS feed
- Uses NVIDIA NIM for AI ranking and summarization
- Publishes directly to Notion
- Prevents duplicate same-day rows and filters repeated items across recent history
- Provides debug endpoints for each source
- Provides status/config-check endpoints for monitoring

## Tech Stack

- Python
- GitHub Actions
- Cloudflare Workers for optional HTTP/debug endpoints
- Notion API
- NVIDIA NIM API
- arXiv API
- Hacker News Algolia Search API
- Hugging Face Hub API
- Techmeme RSS feed
- Wrangler

## Notion Database Schema

The Notion database should contain these properties:

| Property | Type | Purpose |
|---|---|---|
| Date | Date | Digest date |
| Type | Select | `paper` or `news` |
| Title | Title | Item title |
| Description | Text / Rich text | Summary |
| Source | Select | Source name |
| Link | URL | Original source link |
| Status | Status | Defaults to `NOT YET` for every published item |
| Rank | Text | `P1-P5` or `N1-N10` |

Recommended `Source` select options:

```text
arXiv
hacker news
hugging face
techmeme
```

Recommended `Type` select options:

```text
paper
news
```

## GitHub Actions Schedule

The workflow in `.github/workflows/daily-brief.yml` has two daily schedules.

Current schedule:

```text
0 6 * * *
0 12 * * *
```

The primary digest runs at `06:00 UTC`. The `12:00 UTC` reconciliation run uses the same Date + Rank and all-history identity guards, so it adds only items that were missing from the primary run. The workflow can also be started manually from the repository's **Actions** tab. arXiv requests retry transient failures before a run is treated as unavailable.

## Required Secrets

The publishing workflow needs these secrets:

```text
NOTION_TOKEN
NOTION_DATABASE_ID
NVIDIA_NIM_API_KEY
```

Add them at **GitHub repository → Settings → Secrets and variables → Actions → New repository secret**.

Use these exact names:

```text
NOTION_TOKEN
NOTION_DATABASE_ID
NVIDIA_NIM_API_KEY
```

For local CLI execution, export the same variables in your shell. Cloudflare local development can continue using `.dev.vars`:

```bash
NOTION_TOKEN=your_notion_token_here
NOTION_DATABASE_ID=your_notion_database_id_here
NVIDIA_NIM_API_KEY=your_nvidia_nim_api_key_here
```

Do not commit `.dev.vars`.

## Digest Window Config

News is restricted to the previous calendar day using the digest timezone:

```text
DIGEST_TIMEZONE=Europe/Berlin
```

With this configuration, the digest includes only news whose source timestamp falls between `00:00` (inclusive) and the next `00:00` (exclusive) for the previous calendar day in `Europe/Berlin`.

Research papers use a seven-day lookback because arXiv announces submissions Sunday through Thursday and can publish a weekend batch after the papers' original submission dates. The Worker selects the newest unseen papers from that lookback and checks their normalized links and titles against all existing Notion history before publishing them. arXiv papers use their original `published` timestamp, not a later update timestamp.

Notes:

- Hacker News uses the story timestamp.
- Hugging Face uses `lastModified` first, then `createdAt`.
- Techmeme uses each RSS item's publication timestamp.

## Local Development

Install dependencies:

```bash
python -m pip install .
```

Build a digest locally without publishing:

```bash
python src/cli.py
```

Publish missing rows locally:

```bash
python src/cli.py --publish
```

The `--publish` flag is intentionally required. Without it, the CLI does not create Notion rows.

To run the optional Cloudflare HTTP endpoints locally, install the Node dependencies and run `npm run dev`.

## Testing Locally

Health check:

```bash
curl http://localhost:8787/health
```

Status check:

```bash
curl http://localhost:8787/status
```

Config/secrets check:

```bash
curl http://localhost:8787/config-check
```

Build digest without publishing:

```bash
curl -X POST http://localhost:8787/run-digest
```

Publish digest to Notion:

```bash
curl -X POST http://localhost:8787/publish-digest
```

Test scheduled publish simulation:

```bash
curl -X POST http://localhost:8787/debug/scheduled
```

Test one NVIDIA NIM summary:

```bash
curl -X POST http://localhost:8787/debug/nim
```

## Debug Endpoints

The Worker includes source-specific debug endpoints.

```text
GET /debug/arxiv
GET /debug/hackernews
GET /debug/huggingface
GET /debug/techmeme
```

Example:

```bash
curl http://localhost:8787/debug/arxiv
```

These endpoints help verify each source independently.

## Production Endpoints

After deployment, replace the local URL with the deployed Cloudflare Worker URL.

Example:

```bash
curl https://your-worker.your-subdomain.workers.dev/status
```

Main production endpoints:

```text
GET  /health
GET  /status
GET  /config-check
POST /run-digest
POST /publish-digest
POST /debug/scheduled
```

## Deployment

Commit and push `.github/workflows/daily-brief.yml` to the repository's default branch. Add the three GitHub Actions secrets, then run **AI Daily Brief** once using **Actions → Run workflow**.

The Cloudflare Worker can still be deployed for its HTTP/debug endpoints:

```bash
npm run deploy
```

Its cron list is intentionally empty to prevent duplicate publishing and free-tier CPU failures.

## Duplicate Protection

The Worker uses multiple layers of duplicate protection.

Before publishing, it checks whether a row already exists for the same:

```text
Date + Rank
```

That makes manual retries safe for the same digest day.

It also filters out items that already appeared anywhere in Notion history by checking both normalized identities:

```text
Link and Title
```

News is additionally compared by summary similarity, both within the current digest and against all historical Notion descriptions. This catches the same underlying story when a publisher or aggregator uses a different URL or headline.

## NVIDIA NIM Usage

The project uses NVIDIA NIM for:

- ranking AI news candidates
- summarizing missing Notion rows

Current model:

```text
qwen/qwen3-next-80b-a3b-instruct
```

Base URL:

```text
https://integrate.api.nvidia.com/v1
```

NIM summaries are kept concise so the Notion `Description` field stays readable.

## Source Behavior

### arXiv

Uses the arXiv API to fetch recent papers from AI/ML/NLP-related categories.

Example categories:

```text
cs.AI
cs.CL
cs.LG
stat.ML
```

### Hacker News

Uses the Hacker News Algolia Search API (`https://hn.algolia.com/api/v1/search_by_date`).

The pipeline queries Hacker News stories for the exact digest window with paginated search requests, then keeps AI-related items based on keywords and ranks them by engagement.

### Hugging Face

Uses the Hugging Face Hub API to fetch trending model updates.

These are treated as news/model update items.

### Techmeme

Uses Techmeme's RSS feed so every candidate has its own publication timestamp for strict daily-window filtering.

## Project Structure

```text
src/
  cli.py
  entry.py
  http_client.py
  digest.py
  filtering.py
  nim.py
  notion.py
  sources/
    __init__.py
    arxiv.py
    hackernews.py
    huggingface.py
    techmeme.py
wrangler.jsonc
package.json
pyproject.toml
.github/workflows/daily-brief.yml
```

## File Overview

### `src/entry.py`

Main Cloudflare Worker entrypoint.

Handles:

- HTTP routes
- debug endpoints
- publish flow orchestration

### `src/cli.py`

Main GitHub Actions and local command-line entrypoint. It defaults to a dry run and publishes only when `--publish` is supplied.

### `src/http_client.py`

Provides one async HTTP interface for both standard CPython and Cloudflare's Python runtime.

### `src/digest.py`

Builds the daily digest.

Handles:

- fetching all sources
- deduplication
- initial paper ranking
- news ranking with NVIDIA NIM

### `src/filtering.py`

Contains AI keyword filtering logic.

Used to keep source results focused on AI-related content.

### `src/nim.py`

Handles NVIDIA NIM calls.

Used for:

- news ranking
- item summaries

### `src/notion.py`

Handles Notion publishing.

Used for:

- creating Notion rows
- checking existing rows
- preventing duplicates

### `src/sources/arxiv.py`

Fetches recent AI-related papers from arXiv.

### `src/sources/hackernews.py`

Fetches and filters AI-related Hacker News stories.

### `src/sources/huggingface.py`

Fetches trending Hugging Face model updates.

### `src/sources/techmeme.py`

Parses Techmeme's RSS feed and filters AI-related stories.

## Safety Notes

Secrets are not stored in the repository.

Ignored files include:

```text
.dev.vars
.env
.wrangler/
node_modules/
python_modules/
.venv/
.venv-workers/
__pycache__/
```

Before pushing to GitHub, verify secrets are not tracked:

```bash
git ls-files .dev.vars .env
```

This command should print nothing.

## Current Status

This project currently supports:

- GitHub Actions scheduled execution
- source collection
- AI filtering
- deduplication
- NIM-powered news ranking
- NIM-powered summaries
- Notion publishing
- duplicate protection
- debug/status endpoints

## Future Improvements

Possible next improvements:

- add failure alerts through email, Telegram, or Discord
- improve paper ranking with NVIDIA NIM
- update existing Notion rows instead of only skipping them
- create one daily summary page in Notion
- improve Techmeme parsing
- store run logs in a database or Notion log table
- add GitHub Actions failure notifications
- add tests for source parsers
