# AI Daily Brief

AI Daily Brief is a fully automated daily AI news and research brief.

It collects AI-related research papers and news from multiple sources, ranks the most important items, summarizes them using NVIDIA NIM, and publishes the final brief to a Notion database.

The project runs on Cloudflare Workers with a daily cron trigger, so it continues working even when your laptop is off.

## What This Project Does

Every day, this project publishes:

- Top 5 AI research papers
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
Cloudflare Cron Trigger
        |
        v
Cloudflare Worker
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
- Uses Cloudflare Workers free tier
- Uses Cloudflare Cron Triggers
- Collects papers from arXiv
- Collects AI stories from Hacker News
- Collects trending model updates from Hugging Face
- Scrapes AI-related Techmeme stories
- Uses NVIDIA NIM for AI ranking and summarization
- Publishes directly to Notion
- Prevents duplicate same-day rows and filters repeated items across recent history
- Provides debug endpoints for each source
- Provides status/config-check endpoints for monitoring

## Tech Stack

- Python
- Cloudflare Workers
- Cloudflare Cron Triggers
- Notion API
- NVIDIA NIM API
- arXiv API
- Hacker News Firebase API
- Hugging Face Hub API
- Techmeme scraping
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
| Status | Status | Optional Notion workflow status |
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

## Cloudflare Cron Schedule

The Worker is configured to run daily using Cloudflare Cron Triggers.

Current schedule:

```text
0 6 * * *
```

This runs every day at `06:00 UTC`.

## Required Secrets

The Worker needs these secrets:

```text
NOTION_TOKEN
NOTION_DATABASE_ID
NVIDIA_NIM_API_KEY
```

Add them to Cloudflare using Wrangler:

```bash
wrangler secret put NOTION_TOKEN
wrangler secret put NOTION_DATABASE_ID
wrangler secret put NVIDIA_NIM_API_KEY
```

For local development, create a `.dev.vars` file:

```bash
NOTION_TOKEN=your_notion_token_here
NOTION_DATABASE_ID=your_notion_database_id_here
NVIDIA_NIM_API_KEY=your_nvidia_nim_api_key_here
```

Do not commit `.dev.vars`.

## Digest Window Config

The news section can be restricted to a fixed daily window using runtime vars:

```text
DIGEST_TIMEZONE=Europe/Berlin
DIGEST_WINDOW_START_HOUR=8
```

With this configuration, the digest includes only news whose normalized timestamp falls between the previous day at `08:00` and the current day at `07:59:59` in `Europe/Berlin`.

Notes:

- Hacker News uses the story timestamp.
- Hugging Face uses `lastModified` first, then `createdAt`.
- Techmeme uses the snapshot timestamp shown on the fetched page.

## Local Development

Install dependencies:

```bash
npm install
```

Run the Worker locally:

```bash
wrangler dev
```

The local server usually runs at:

```text
http://localhost:8787
```

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

Deploy to Cloudflare:

```bash
wrangler deploy
```

After deployment, Cloudflare runs the Worker automatically on the configured cron schedule.

## Duplicate Protection

The Worker uses two layers of duplicate protection.

Before publishing, it checks whether a row already exists for the same:

```text
Date + Rank
```

That makes manual retries safe for the same digest day.

It also filters out items that already appeared in recent Notion history by using a normalized item identity based on:

```text
Link first, then Title as a fallback
```

This helps prevent the same paper or news item from appearing again on later days.

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

Uses the public Hacker News Firebase API.

The Worker scans recent top stories and keeps AI-related items based on keywords.

### Hugging Face

Uses the Hugging Face Hub API to fetch trending model updates.

These are treated as news/model update items.

### Techmeme

Uses lightweight HTML scraping.

Because Techmeme does not provide a stable public API, this source is the most likely to need future maintenance if the page structure changes.

For the digest window filter, Techmeme items use the snapshot timestamp shown on the fetched Techmeme page.

## Project Structure

```text
src/
  entry.py
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
```

## File Overview

### `src/entry.py`

Main Cloudflare Worker entrypoint.

Handles:

- HTTP routes
- debug endpoints
- scheduled cron execution
- publish flow orchestration

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

Scrapes Techmeme and filters AI-related stories.

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

- cloud-hosted daily execution
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
- add GitHub Actions checks
- add tests for source parsers
