# AI Daily Brief

AI Daily Brief is a fully automated daily AI news brief.

It collects AI news and model updates from multiple sources, ranks the most relevant items, summarizes them using NVIDIA NIM, and publishes the final brief to a Notion database.

A lightweight Cloudflare scheduler starts the GitHub Actions pipeline at **07:55 Europe/Berlin**. The pipeline prepares the news and holds Notion publishing until **08:00**, with automatic daylight-saving adjustment. The separate Python Worker provides optional health, status, and source-debug endpoints.

## What This Project Does

For each digest date, this project publishes up to **10 ranked AI news/model updates**.

These are maximum counts. Source failures, limited eligible content, duplicate checks, and summary validation can reduce the number published. Manual retries fill available ranks without replacing existing rows.

The final output is written into a Notion database with ranks like:

- `N1`, `N2`, ..., `N10` for news

## Data Sources

The pipeline collects news from:

- Hacker News
- Hugging Face
- Techmeme

Each source is normalized into a common item format before ranking and publishing.

## How It Works

The full pipeline looks like this:

```text
Cloudflare Cron (07:55 Europe/Berlin)
        |
        v
GitHub Actions Dispatch
        |
        v
Python CLI
        |
        v
Collect from Hacker News, Hugging Face, Techmeme
        |
        v
Filter AI-related content
        |
        v
Deduplicate repeated links
        |
        v
Rank eligible news by relevance
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
- Hacker News Algolia Search API
- Hugging Face Hub API
- Techmeme RSS feed
- Wrangler

## Notion Database Schema

The Notion database should contain these properties:

| Property | Type | Purpose |
|---|---|---|
| Date | Date | Digest date |
| Type | Select | `news` |
| Title | Title | Item title |
| Description | Text / Rich text | Summary |
| Source | Select | Source name |
| Link | URL | Original source link |
| Status | Status | Defaults to `NOT YET` for every published item |
| Rank | Text | `N1-N10` |

Recommended `Source` select options:

```text
hacker news
hugging face
techmeme
```

Recommended `Type` select options:

```text
news
```

## Morning Schedule

GitHub's cron scheduler proved unsuitable for punctual delivery: on November 2, 2026, the 08:00 Berlin run was not created until 17:38. The pipeline itself finished publishing within seconds. The primary clock is now the independent Worker in `scheduler/`:

1. **07:55 Berlin:** Cloudflare calls GitHub's workflow-dispatch API with the digest date and an 08:00 release timestamp. GitHub prepares the digest immediately.
2. **07:58 Berlin:** Cloudflare checks whether that day's dispatch is queued, running, or successful. It dispatches a retry only when no such run exists.
3. **08:00 Berlin:** The prepared pipeline starts writing missing news rows to Notion.
4. **08:05 Berlin:** GitHub's own cron remains a best-effort recovery path. If GitHub delays it, existing Date + Rank and history checks prevent duplicate publishing.

Cloudflare cron uses UTC, so `55,58 5,6 * * *` checks both possible UTC hours. The Worker converts the scheduled timestamp to `Europe/Berlin` and ignores the inactive hour, handling CET/CEST automatically. The existing Python Worker's cron remains disabled; the lightweight JavaScript scheduler only dispatches GitHub work and does not run the CPU-heavy digest pipeline.

The release timestamp is passed as `--release-at`. Sources, ranking, and summaries are prepared first, then an async wait holds only Notion writes until the requested time. Past release timestamps run immediately, and waits over 15 minutes are rejected. Manual runs without a release timestamp publish immediately.

This removes reliance on delayed GitHub cron for the primary run and gives GitHub five minutes of preparation time. GitHub runner outages and external APIs can still cause late delivery; this is not a hard real-time delivery guarantee. The existing manual date override and duplicate guards remain available. See [GitHub's scheduling limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

### Deploy the Scheduler

The scheduler requires a GitHub credential stored as Cloudflare secret `GITHUB_WORKFLOW_TOKEN`. A fine-grained token restricted to this repository with **Actions: read and write** is sufficient. The scheduler never needs the Notion or NVIDIA secrets, which stay in GitHub Actions. Token expiration or revocation requires replacing the Cloudflare secret.

```bash
npx wrangler deploy --config scheduler/wrangler.jsonc
npx wrangler secret put GITHUB_WORKFLOW_TOKEN --config scheduler/wrangler.jsonc
```

Enter the credential at the secure prompt; never put it in source or command arguments. Check `/health` on the scheduler's deployed URL to verify the secret is configured. Only the health route is public; HTTP requests cannot trigger publishing. Cloudflare cron changes can take up to 15 minutes to propagate.

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

Research-paper collection and publishing have been removed. Existing paper rows in Notion remain intact; new rows use only `news` and ranks `N1`–`N10`. No Notion database migration is required.

Notes:

- Hacker News uses the story timestamp.
- Hugging Face uses `lastModified` first, then `createdAt`.
- Techmeme uses each RSS item's publication timestamp.

## Local Development

Run these commands from the repository root. Install Python 3.12 or newer, then create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install .
```

Export all three variables listed under **Required Secrets**, including for a CLI dry run. Dry runs read Notion history and call the source and NIM APIs, but do not create Notion rows. The CLI does not automatically load `.env` or `.dev.vars` files.

Build a digest locally without publishing:

```bash
python src/cli.py
```

Publish missing rows locally:

```bash
python src/cli.py --publish
```

The `--publish` flag is intentionally required. Without it, the CLI does not create Notion rows.

### Backfill a Publish Date

Use `--date YYYY-MM-DD` to override the publish date:

```bash
python src/cli.py --date 2026-10-13
python src/cli.py --publish --date 2026-10-13
```

The first command previews the backfill; the second publishes missing rows. The date is the Notion digest date. News covers the previous calendar day in `DIGEST_TIMEZONE` (default: `Europe/Berlin`), so this example covers news from October 12. Existing Date + Rank and all-history duplicate checks still apply.

To backfill through GitHub Actions, open **Actions → AI Daily Brief → Run workflow** and enter the optional `date` input in `YYYY-MM-DD` format. Leave it blank to use the current publish date. Backfills query the live sources, so older content may no longer be available.

### Optional Cloudflare Worker Setup

Install Node.js with npm and [uv](https://docs.astral.sh/uv/getting-started/installation/). The npm scripts call `uv run pywrangler`, which uses the `workers-py` development dependency in `pyproject.toml`. See the [Cloudflare Python Workers setup guide](https://developers.cloudflare.com/workers/languages/python/).

Install the Node dependencies, put the three secrets in `.dev.vars`, and start the HTTP endpoints:

```bash
npm install
npm run dev
```

For deployment, authenticate with Cloudflare and configure the same secrets on the deployed Worker; `.dev.vars` supplies local development secrets only.

## Testing Locally

Run the automated regression suite from the repository root:

```bash
python -m unittest discover -s tests -v
node --test scheduler/index.test.mjs
```

The suite covers news-only collection and publishing, relevance ranking across all candidates, CLI dry runs and date overrides, Berlin scheduling and DST windows, Worker routes, Techmeme RSS timestamps, duplicate protection, Notion properties, and source failure handling. API calls are mocked, so the tests do not require secrets or publish to Notion.

For the following HTTP checks, first start the optional Worker with `npm run dev`.

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

Run the scheduled publishing flow manually (creates missing rows in the configured Notion database):

```bash
curl -X POST http://localhost:8787/debug/scheduled
```

Despite the `debug` name and the response's "simulation" wording, `/debug/scheduled` performs real Notion writes. Use `/run-digest` to build a digest without publishing.

Test one NVIDIA NIM summary:

```bash
curl -X POST http://localhost:8787/debug/nim
```

## Debug Endpoints

The Worker includes source-specific debug endpoints.

```text
GET /debug/hackernews
GET /debug/huggingface
GET /debug/techmeme
```

Example:

```bash
curl http://localhost:8787/debug/hackernews
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

Both `/publish-digest` and `/debug/scheduled` create missing Notion rows. `/run-digest` builds a digest without creating rows.

## Deployment

Commit and push `.github/workflows/daily-brief.yml` to the repository's default branch. Add the three GitHub Actions secrets, then run **AI Daily Brief** once using **Actions → Run workflow**.

The Cloudflare Worker can still be deployed for its HTTP/debug endpoints:

```bash
npm run deploy
```

The Python Worker's cron list is intentionally empty to prevent free-tier CPU failures. The separate `scheduler/wrangler.jsonc` owns the lightweight morning dispatch triggers.

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

All eligible, unseen news candidates are sent to NIM for relevance ranking rather than being cut down to a cross-source engagement shortlist. NIM prioritizes significant releases, credible sources, meaningful impact, originality, and broad relevance. If ranking fails, the pipeline falls back to local scores.

NIM summaries are kept concise so the Notion `Description` field stays readable.

## Source Behavior

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
scheduler/
  index.mjs
  index.test.mjs
  wrangler.jsonc
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
    hackernews.py
    huggingface.py
    techmeme.py
tests/
  test_pipeline.py
  test_entry.py
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

- Cloudflare-timed GitHub Actions execution with an 08:00 Berlin release gate
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
- update existing Notion rows instead of only skipping them
- create one daily summary page in Notion
- improve Techmeme parsing
- store run logs in a database or Notion log table
- add GitHub Actions failure notifications
- expand source parser coverage for Hacker News and Hugging Face
