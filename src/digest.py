from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from notion import get_recent_item_identities, item_identity
from sources.arxiv import fetch_arxiv_papers
from sources.hackernews import fetch_hackernews_ai_stories
from sources.huggingface import fetch_huggingface_ai_models
from sources.techmeme import fetch_techmeme_ai_stories
from nim import rank_news_with_nim


DEFAULT_DIGEST_TIMEZONE = "UTC"
DEFAULT_WINDOW_START_HOUR = 8


def _rank_items(items, prefix, limit):
    ranked_items = sorted(
        items,
        key=lambda item: (
            not item.get("recently_seen", False),
            item.get("score_hint", 0) or 0,
        ),
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


def _mark_recently_seen(items, existing_identities):
    marked_items = []

    for item in items:
        identity = item_identity(item)
        item["recently_seen"] = identity in existing_identities if identity else False
        marked_items.append(item)

    return marked_items


def _digest_timezone(env):
    timezone_name = getattr(env, "DIGEST_TIMEZONE", None) or DEFAULT_DIGEST_TIMEZONE

    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(DEFAULT_DIGEST_TIMEZONE)


def _window_start_hour(env):
    raw_value = getattr(env, "DIGEST_WINDOW_START_HOUR", None)

    if raw_value is None:
        return DEFAULT_WINDOW_START_HOUR

    try:
        hour = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_START_HOUR

    return hour if 0 <= hour <= 23 else DEFAULT_WINDOW_START_HOUR


def _news_window(env, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    digest_tz = _digest_timezone(env)
    local_now = now_utc.astimezone(digest_tz)
    start_hour = _window_start_hour(env)
    boundary_today = local_now.replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    window_end_local = (
        boundary_today
        if local_now >= boundary_today
        else boundary_today - timedelta(days=1)
    )
    window_start_local = window_end_local - timedelta(days=1)

    return {
        "timezone": str(digest_tz),
        "start_hour": start_hour,
        "window_start_local": window_start_local,
        "window_end_local": window_end_local,
        "window_start_utc": window_start_local.astimezone(timezone.utc),
        "window_end_utc": window_end_local.astimezone(timezone.utc),
    }


def _parse_timestamp(value):
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)

    text = str(value).strip()

    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _item_timestamp_for_window(item):
    source = (item.get("source") or "").lower()
    timestamp_candidates = []

    if source == "hugging face":
        timestamp_candidates = [item.get("updated_at"), item.get("published_at")]
    else:
        timestamp_candidates = [item.get("published_at"), item.get("updated_at")]

    parsed_candidates = [
        parsed
        for parsed in (_parse_timestamp(value) for value in timestamp_candidates)
        if parsed is not None
    ]

    if not parsed_candidates:
        return None

    return max(parsed_candidates)


def _filter_items_to_window(items, window_start_utc, window_end_utc):
    included_items = []
    skipped_without_timestamp = 0
    skipped_outside_window = 0

    for item in items:
        item_timestamp = _item_timestamp_for_window(item)

        if item_timestamp is None:
            skipped_without_timestamp += 1
            continue

        if not (window_start_utc <= item_timestamp < window_end_utc):
            skipped_outside_window += 1
            continue

        item["window_timestamp"] = item_timestamp.isoformat()
        included_items.append(item)

    return {
        "items": included_items,
        "skipped_without_timestamp": skipped_without_timestamp,
        "skipped_outside_window": skipped_outside_window,
    }


async def build_digest(env=None):
    digest_env = env
    window = _news_window(digest_env)
    recent_identities = (
        await get_recent_item_identities(env)
        if env
        else set()
    )

    papers = await fetch_arxiv_papers(limit=60)
    hn_news = await fetch_hackernews_ai_stories(limit=5, scan_count=8)
    hf_news = await fetch_huggingface_ai_models(limit=30, scan_count=80)
    techmeme_news = await fetch_techmeme_ai_stories(limit=20)

    deduped_news = _dedupe_by_url([*hn_news, *hf_news, *techmeme_news])
    windowed_news_result = _filter_items_to_window(
        deduped_news,
        window["window_start_utc"],
        window["window_end_utc"],
    )
    eligible_papers = _mark_recently_seen(papers, recent_identities)
    all_news = _mark_recently_seen(windowed_news_result["items"], recent_identities)
    ranked_papers = _rank_items(eligible_papers, "P", 5)
    locally_ranked_news = _rank_items(all_news, "N", 20)
    ranked_news = (
        await rank_news_with_nim(env, locally_ranked_news, limit=10)
        if env
        else _rank_items(all_news, "N", 10)
    )

    return {
        "ok": True,
        "date": window["window_end_local"].date().isoformat(),
        "papers": ranked_papers,
        "news": ranked_news,
        "stats": {
            "papers_seen": len(papers),
            "hackernews_seen": len(hn_news),
            "huggingface_seen": len(hf_news),
            "techmeme_seen": len(techmeme_news),
            "news_after_dedupe": len(deduped_news),
            "news_in_window": len(all_news),
            "news_missing_timestamp": windowed_news_result["skipped_without_timestamp"],
            "news_outside_window": windowed_news_result["skipped_outside_window"],
            "news_window_timezone": window["timezone"],
            "news_window_start_hour": window["start_hour"],
            "news_window_start": window["window_start_local"].isoformat(),
            "news_window_end": window["window_end_local"].isoformat(),
            "recent_identities_seen": len(recent_identities),
            "papers_already_seen_recently": sum(
                1 for item in eligible_papers if item.get("recently_seen")
            ),
            "news_already_seen_recently": sum(
                1 for item in all_news if item.get("recently_seen")
            ),
        },
    }
