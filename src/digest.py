import asyncio
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from notion import get_published_items, item_identities
from sources.arxiv import fetch_arxiv_papers
from sources.hackernews import fetch_hackernews_ai_stories
from sources.huggingface import fetch_huggingface_ai_models
from sources.techmeme import fetch_techmeme_ai_stories
from nim import rank_news_with_nim


DEFAULT_DIGEST_TIMEZONE = "UTC"
PAPER_LOOKBACK_DAYS = 7
TITLE_STOPWORDS = {
    "a", "an", "and", "announces", "announcing", "at", "for", "from", "in",
    "into", "its", "new", "of", "on", "the", "to", "with",
}


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


def _normalize_title(value):
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    return " ".join(normalized.split())


def _normalize_url(value):
    url = str(value or "").strip()

    if not url:
        return ""

    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path.rstrip("/") or "/"

    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        query,
        "",
    ))


def _title_similarity(left, right):
    return SequenceMatcher(
        None,
        _normalize_title(left),
        _normalize_title(right),
    ).ratio()


def _significant_title_tokens(value):
    return {
        token
        for token in _normalize_title(value).split()
        if len(token) >= 4 and token not in TITLE_STOPWORDS
    }


def _domain(url):
    return urlsplit(str(url or "")).netloc.lower()


def _is_exact_duplicate_news(left, right):
    left_url = _normalize_url(left.get("url"))
    right_url = _normalize_url(right.get("url"))

    if left_url and left_url == right_url:
        return True

    left_title = _normalize_title(left.get("title"))
    right_title = _normalize_title(right.get("title"))

    return bool(left_title and left_title == right_title)


def _is_likely_duplicate_news(left, right):
    if _is_exact_duplicate_news(left, right):
        return True

    similarity = _title_similarity(left.get("title"), right.get("title"))
    if similarity >= 0.9:
        return True

    left_tokens = _significant_title_tokens(left.get("title"))
    right_tokens = _significant_title_tokens(right.get("title"))

    if not left_tokens or not right_tokens:
        return False

    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0

    if jaccard >= 0.75:
        return True

    if overlap >= 3 and jaccard >= 0.6:
        return True

    same_domain = (
        _domain(left.get("url"))
        and _domain(left.get("url")) == _domain(right.get("url"))
    )
    return same_domain and similarity >= 0.72 and jaccard >= 0.5


def _candidate_priority(item):
    timestamp = _item_timestamp_for_window(item)
    timestamp_value = timestamp.timestamp() if timestamp else 0
    return (
        item.get("score_hint", 0) or 0,
        timestamp_value,
        len(str(item.get("summary_input", "") or "")),
    )


async def _dedupe_news_candidates(env, items):
    kept_items = []
    skipped_exact = []
    skipped_semantic = []

    for item in sorted(items, key=_candidate_priority, reverse=True):
        duplicate_of = None
        duplicate_reason = None

        for existing in kept_items:
            if _is_exact_duplicate_news(item, existing):
                duplicate_of = existing
                duplicate_reason = "exact"
                break

            if _is_likely_duplicate_news(item, existing):
                duplicate_of = existing
                duplicate_reason = "similar_title"
                break

        if duplicate_of:
            item["pre_rank_duplicate_of"] = duplicate_of.get("title")
            item["pre_rank_duplicate_reason"] = duplicate_reason

            if duplicate_reason == "exact":
                skipped_exact.append(item)
            else:
                skipped_semantic.append(item)
            continue

        kept_items.append(item)

    return {
        "items": kept_items,
        "skipped_exact": skipped_exact,
        "skipped_semantic": skipped_semantic,
    }


def _exclude_previously_published(items, existing_identities):
    unseen_items = []
    seen_items = []

    for item in items:
        identities = item_identities(item)

        if identities & existing_identities:
            item["recently_seen"] = True
            seen_items.append(item)
            continue

        item["recently_seen"] = False
        unseen_items.append(item)

    return {
        "items": unseen_items,
        "skipped": seen_items,
    }


async def _dedupe_news_against_history(env, items, published_items):
    historical_news = [
        item for item in published_items
        if item.get("type") == "news"
    ]
    unique_items = []
    skipped_items = []

    for item in items:
        duplicate_of = None
        duplicate_reason = None

        for historical_item in historical_news:
            if _is_exact_duplicate_news(item, historical_item):
                duplicate_of = historical_item
                duplicate_reason = "exact_history"
                break

            if _is_likely_duplicate_news(item, historical_item):
                duplicate_of = historical_item
                duplicate_reason = "similar_title_history"
                break

        if duplicate_of:
            item["history_duplicate_of"] = duplicate_of.get("title")
            item["history_duplicate_reason"] = duplicate_reason
            skipped_items.append(item)
            continue

        unique_items.append(item)

    return {
        "items": unique_items,
        "skipped": skipped_items,
    }


def _digest_timezone(env):
    timezone_name = getattr(env, "DIGEST_TIMEZONE", None) or DEFAULT_DIGEST_TIMEZONE

    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(DEFAULT_DIGEST_TIMEZONE)


def _news_window(env, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    digest_tz = _digest_timezone(env)
    local_now = now_utc.astimezone(digest_tz)
    window_end_local = local_now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    window_start_local = window_end_local - timedelta(days=1)

    return {
        "timezone": str(digest_tz),
        "window_kind": "previous_calendar_day",
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

    for value in timestamp_candidates:
        parsed = _parse_timestamp(value)

        if parsed is not None:
            return parsed

    return None


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


def _paper_lookback_window(news_window):
    window_end_local = news_window["window_end_local"]
    window_start_local = window_end_local - timedelta(days=PAPER_LOOKBACK_DAYS)

    return {
        "days": PAPER_LOOKBACK_DAYS,
        "window_start_local": window_start_local,
        "window_end_local": window_end_local,
        "window_start_utc": window_start_local.astimezone(timezone.utc),
        "window_end_utc": news_window["window_end_utc"],
    }


async def build_digest(env=None, now_utc=None, published_items=None):
    digest_env = env
    window = _news_window(digest_env, now_utc=now_utc)
    paper_window = _paper_lookback_window(window)
    if published_items is None:
        published_items = await get_published_items(env) if env else []
    published_identities = set()

    for item in published_items:
        published_identities.update(item_identities(item))

    source_names = ("arxiv", "hackernews", "huggingface", "techmeme")
    source_results = await asyncio.gather(
        fetch_arxiv_papers(limit=200),
        fetch_hackernews_ai_stories(
            limit=40,
            scan_count=1000,
            window_start_utc=window["window_start_utc"],
            window_end_utc=window["window_end_utc"],
        ),
        fetch_huggingface_ai_models(limit=50, scan_count=100),
        fetch_techmeme_ai_stories(limit=50),
        return_exceptions=True,
    )
    source_errors = {
        source_name: str(result)
        for source_name, result in zip(source_names, source_results)
        if isinstance(result, Exception)
    }
    papers, hn_news, hf_news, techmeme_news = [
        [] if isinstance(result, Exception) else result
        for result in source_results
    ]

    deduped_news = _dedupe_by_url([*hn_news, *hf_news, *techmeme_news])
    windowed_papers_result = _filter_items_to_window(
        papers,
        paper_window["window_start_utc"],
        paper_window["window_end_utc"],
    )
    windowed_news_result = _filter_items_to_window(
        deduped_news,
        window["window_start_utc"],
        window["window_end_utc"],
    )
    unseen_papers_result = _exclude_previously_published(
        windowed_papers_result["items"],
        published_identities,
    )
    unseen_news_result = _exclude_previously_published(
        windowed_news_result["items"],
        published_identities,
    )
    history_dedupe_result = await _dedupe_news_against_history(
        env,
        unseen_news_result["items"],
        published_items,
    )
    pre_rank_dedupe_result = await _dedupe_news_candidates(
        env,
        history_dedupe_result["items"],
    )
    eligible_papers = unseen_papers_result["items"]
    all_news = pre_rank_dedupe_result["items"]
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
        "content_date": window["window_start_local"].date().isoformat(),
        "papers": ranked_papers,
        "news": ranked_news,
        "stats": {
            "papers_seen": len(papers),
            "source_errors": source_errors,
            "papers_in_lookback": len(windowed_papers_result["items"]),
            "papers_considered": len(eligible_papers),
            "papers_missing_timestamp": windowed_papers_result["skipped_without_timestamp"],
            "papers_outside_lookback": windowed_papers_result["skipped_outside_window"],
            "paper_lookback_days": paper_window["days"],
            "paper_lookback_start": paper_window["window_start_local"].isoformat(),
            "paper_lookback_end": paper_window["window_end_local"].isoformat(),
            "hackernews_seen": len(hn_news),
            "huggingface_seen": len(hf_news),
            "techmeme_seen": len(techmeme_news),
            "news_after_dedupe": len(deduped_news),
            "news_after_history_dedupe": len(history_dedupe_result["items"]),
            "news_after_pre_rank_dedupe": len(pre_rank_dedupe_result["items"]),
            "news_in_window": len(all_news),
            "news_missing_timestamp": windowed_news_result["skipped_without_timestamp"],
            "news_outside_window": windowed_news_result["skipped_outside_window"],
            "news_skipped_pre_rank_exact": len(pre_rank_dedupe_result["skipped_exact"]),
            "news_skipped_pre_rank_semantic": len(pre_rank_dedupe_result["skipped_semantic"]),
            "news_skipped_history_identity": len(unseen_news_result["skipped"]),
            "news_skipped_history_semantic": len(history_dedupe_result["skipped"]),
            "news_window_timezone": window["timezone"],
            "news_window_kind": window["window_kind"],
            "news_window_start": window["window_start_local"].isoformat(),
            "news_window_end": window["window_end_local"].isoformat(),
            "published_items_checked": len(published_items),
            "published_identities_checked": len(published_identities),
            "papers_skipped_history_identity": len(unseen_papers_result["skipped"]),
        },
    }
