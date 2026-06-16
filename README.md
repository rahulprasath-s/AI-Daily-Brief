# AI Daily Brief

AI Daily Brief is a fully automated daily AI news and research brief.

It collects AI-related research papers and news from multiple sources, ranks the most important items, summarizes them using Qwen3-80b model(via Nvidia NIM), and publishes the final brief to a Notion database.

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
Use Qwen3 model for news ranking and summaries
        |
        v
Check Notion for existing Date + Rank rows
        |
        v
Publish missing rows to Notion
