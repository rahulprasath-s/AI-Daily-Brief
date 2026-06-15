from datetime import datetime, timezone

from sources.arxiv import fetch_arxiv_papers
from sources.hackernews import fetch_hackernews_ai_stories
from sources.huggingface import fetch_huggingface_ai_models
from sources.techmeme import fetch_techmeme_ai_stories
from nim import rank_news_with_nim


def _rank_items(items, prefix, limit):
    ranked_items = sorted(
        items,
        key=lambda item: item.get("score_hint", 0) or 0,
        reverse=True,
    )[:limit]

    for index, item in enumerate(ranked_items, start=1):
        item["rank"] = f"{prefix}{index}"

    return ranked_items


def _dedupe_by_url(items):
    seen_urls = set()
    unique_items = []

    for item in items:
        url = item.get("url")

        if not url or url in seen_urls:
            continue

        seen_urls.add(url)
        unique_items.append(item)

    return unique_items


async def build_digest(env=None):
    papers = await fetch_arxiv_papers(limit=12)
    hn_news = await fetch_hackernews_ai_stories(limit=5, scan_count=10)
    hf_news = await fetch_huggingface_ai_models(limit=10)
    techmeme_news = await fetch_techmeme_ai_stories(limit=10)

    all_news = _dedupe_by_url([*hn_news, *hf_news, *techmeme_news])
    ranked_papers = _rank_items(papers, "P", 5)
    locally_ranked_news = _rank_items(all_news, "N", 20)
    ranked_news = (
        await rank_news_with_nim(env, locally_ranked_news, limit=10)
        if env
        else _rank_items(all_news, "N", 10)
    )

    return {
        "ok": True,
        "date": datetime.now(timezone.utc).date().isoformat(),
        "papers": ranked_papers,
        "news": ranked_news,
        "stats": {
            "papers_seen": len(papers),
            "hackernews_seen": len(hn_news),
            "huggingface_seen": len(hf_news),
            "techmeme_seen": len(techmeme_news),
            "news_after_dedupe": len(all_news),
        },
    }
