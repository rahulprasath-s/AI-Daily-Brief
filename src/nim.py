import asyncio
from difflib import SequenceMatcher
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from http_client import fetch


NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_NIM_MODEL = "qwen/qwen3-next-80b-a3b-instruct"
DEFAULT_NIM_TIMEOUT_SECONDS = 15
DEFAULT_NIM_SUMMARY_CONCURRENCY = 4
SUMMARY_SIMILARITY_THRESHOLD = 0.7
BLOCKED_NEWS_SUMMARY_PHRASES = (
    "no valid news",
    "no substantive ai news",
    "no substansive ai news",
    "no substantial ai news",
    "unable to access",
    "unable to view",
    "cannot access",
    "can t access",
    "could not access",
    "content not available",
    "content is not available",
    "article not available",
    "article is not available",
    "source not available",
    "source is not available",
    "source page unavailable",
    "could not retrieve",
    "couldn t retrieve",
    "not enough information",
    "insufficient information",
    "page could not be loaded",
    "website is not visible",
)
TITLE_STOPWORDS = {
    "a", "an", "and", "announces", "announcing", "at", "for", "from", "in",
    "into", "its", "new", "of", "on", "the", "to", "with",
}


def _shorten(text, max_chars=1200):
    text = " ".join(str(text or "").split())
    return text[:max_chars]


def _normalize_summary_text(text):
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    return " ".join(normalized.split())


def _normalize_title(text):
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
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


def summary_similarity(left, right):
    normalized_left = _normalize_summary_text(left)
    normalized_right = _normalize_summary_text(right)

    if not normalized_left or not normalized_right:
        return 0.0

    sequence_similarity = SequenceMatcher(
        None,
        normalized_left,
        normalized_right,
    ).ratio()
    left_tokens = {
        token for token in normalized_left.split()
        if len(token) >= 4 and token not in TITLE_STOPWORDS
    }
    right_tokens = {
        token for token in normalized_right.split()
        if len(token) >= 4 and token not in TITLE_STOPWORDS
    }

    if not left_tokens or not right_tokens:
        return sequence_similarity

    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0
    containment = (
        overlap / min(len(left_tokens), len(right_tokens))
        if overlap >= 5
        else 0.0
    )
    return max(sequence_similarity, jaccard, containment)


def is_publishable_news_item(item):
    if item.get("type", "news") != "news":
        return True

    normalized_summary = _normalize_summary_text(item.get("summary", ""))

    if not normalized_summary:
        return False

    return not any(
        phrase in normalized_summary
        for phrase in BLOCKED_NEWS_SUMMARY_PHRASES
    )


def _chat_payload(messages, max_tokens=180):
    return {
        "model": NVIDIA_NIM_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }


def _nim_timeout_seconds(env):
    raw_value = getattr(env, "NVIDIA_NIM_TIMEOUT_SECONDS", None)

    if raw_value is None:
        return DEFAULT_NIM_TIMEOUT_SECONDS

    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_NIM_TIMEOUT_SECONDS

    return timeout_seconds if timeout_seconds > 0 else DEFAULT_NIM_TIMEOUT_SECONDS


def _nim_summary_concurrency(env):
    raw_value = getattr(env, "NVIDIA_NIM_SUMMARY_CONCURRENCY", None)

    if raw_value is None:
        return DEFAULT_NIM_SUMMARY_CONCURRENCY

    try:
        concurrency = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_NIM_SUMMARY_CONCURRENCY

    return concurrency if concurrency > 0 else DEFAULT_NIM_SUMMARY_CONCURRENCY


async def _chat_completion(nim_api_key, payload, timeout_seconds=DEFAULT_NIM_TIMEOUT_SECONDS):
    url = f"{NVIDIA_NIM_BASE_URL}/chat/completions"
    try:
        response = await asyncio.wait_for(
            fetch(
                url,
                method="POST",
                headers={
                    "authorization": f"Bearer {nim_api_key}",
                    "content-type": "application/json",
                },
                body=json.dumps(payload),
            ),
            timeout=timeout_seconds,
        )
        response_text = await asyncio.wait_for(
            response.text(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"NVIDIA NIM request timed out after {timeout_seconds}s"
        ) from exc

    try:
        data = json.loads(response_text)
    except Exception:
        raise RuntimeError(
            f"NVIDIA NIM returned non-JSON response from {url} "
            f"with status {response.status}: {response_text[:500]}"
        )

    if not response.ok:
        raise RuntimeError(f"NVIDIA NIM request failed: {data}")

    return data["choices"][0]["message"]["content"]


def _prompt_for_item(item):
    item_type = item.get("type", "news")
    title = item.get("title", "")
    source = item.get("source", "")
    content = _shorten(item.get("summary_input", "") or title)

    if item_type == "paper":
        instruction = "Summarize this AI research paper."
    else:
        instruction = "Summarize this AI news/model item."

    return f"""
{instruction}

Return only valid JSON with this exact shape:
{{"summary":"one concise summary under 100 words"}}

Title: {title}
Source: {source}
Content: {content}
""".strip()


def _fallback_summary(item, max_words=40):
    text = item.get("summary_input", "") or item.get("title", "")
    words = " ".join(str(text).split()).split()

    if len(words) <= max_words:
        return " ".join(words)

    return " ".join(words[:max_words]).rstrip(".,;:") + "..."


async def _summarize_item(nim_api_key, item, timeout_seconds=DEFAULT_NIM_TIMEOUT_SECONDS):
    content = await _chat_completion(
        nim_api_key,
        _chat_payload([
            {
                "role": "system",
                "content": "You write factual, concise AI brief summaries. Return only valid JSON.",
            },
            {
                "role": "user",
                "content": _prompt_for_item(item),
            },
        ]),
        timeout_seconds=timeout_seconds,
    )

    try:
        parsed = json.loads(content)
        summary = parsed.get("summary", "")
    except Exception:
        summary = content

    return " ".join(summary.split())


def _ranking_prompt(items, limit):
    candidates = []

    for index, item in enumerate(items):
        candidates.append({
            "id": index,
            "title": item.get("title"),
            "source": item.get("source"),
            "url": item.get("url"),
            "score_hint": item.get("score_hint", 0),
            "recently_seen": bool(item.get("recently_seen")),
            "context": _shorten(item.get("summary_input", ""), 300),
        })

    return f"""
Rank these AI news/model candidates for a daily AI brief.

Prefer: major AI releases, credible sources, meaningful research/product impact, originality, and broad relevance.
Avoid: items marked recently_seen when fresher unseen options exist, duplicates, low-signal model variants, and obscure items unless technically important.

Return only valid JSON with this shape:
{{"ranked_ids":[0,1,2]}}

Return exactly {limit} ids when possible.

Candidates:
{json.dumps(candidates)}
""".strip()


def _duplicate_check_prompt(left, right):
    left_payload = {
        "title": left.get("title"),
        "source": left.get("source"),
        "url": left.get("url"),
        "published_at": left.get("published_at"),
        "updated_at": left.get("updated_at"),
        "context": _shorten(left.get("summary_input", ""), 400),
    }
    right_payload = {
        "title": right.get("title"),
        "source": right.get("source"),
        "url": right.get("url"),
        "published_at": right.get("published_at"),
        "updated_at": right.get("updated_at"),
        "context": _shorten(right.get("summary_input", ""), 400),
    }

    return f"""
Decide whether these two AI news items refer to the same underlying event, release, announcement, model update, or story.

Mark duplicate as true only when they are substantively the same story, even if wording or links differ.
Mark duplicate as false when they are merely related, from the same company, or part of the same broader topic.

Return only valid JSON with this exact shape:
{{"duplicate":true,"confidence":0.0,"reason":"short explanation"}}

Item A:
{json.dumps(left_payload)}

Item B:
{json.dumps(right_payload)}
""".strip()


def _apply_rank(items, prefix, limit):
    ranked = items[:limit]

    for index, item in enumerate(ranked, start=1):
        item["rank"] = f"{prefix}{index}"

    return ranked


async def _find_duplicate_in_selection(env, item, selected_items):
    for existing in selected_items:
        if _is_exact_duplicate_news(item, existing):
            return existing, "exact"

        if not _is_likely_duplicate_news(item, existing):
            continue

        return existing, "similar_title"

    return None, None


async def _select_unique_ranked_news(env, candidates, limit):
    unique_items = []

    for item in candidates:
        if len(unique_items) >= limit:
            break

        duplicate_item, duplicate_reason = await _find_duplicate_in_selection(
            env,
            item,
            unique_items,
        )
        if duplicate_item:
            item["post_rank_duplicate_of"] = duplicate_item.get("title")
            item["post_rank_duplicate_reason"] = duplicate_reason
            item["post_rank_duplicate_of_rank"] = duplicate_item.get("rank")
            continue

        unique_items.append(item)

    return unique_items


async def rank_news_with_nim(env, news_items, limit=10):
    nim_api_key = getattr(env, "NVIDIA_NIM_API_KEY", None)
    timeout_seconds = _nim_timeout_seconds(env)

    if not nim_api_key or not news_items:
        return _apply_rank(news_items, "N", limit)

    try:
        content = await _chat_completion(
            nim_api_key,
            _chat_payload([
                {
                    "role": "system",
                    "content": "You rank AI news for a concise daily brief. Return only valid JSON.",
                },
                {
                    "role": "user",
                    "content": _ranking_prompt(news_items, limit),
                },
            ], max_tokens=220),
            timeout_seconds=timeout_seconds,
        )
        ranked_ids = json.loads(content).get("ranked_ids", [])
        ranked_items = [
            news_items[item_id]
            for item_id in ranked_ids
            if isinstance(item_id, int) and 0 <= item_id < len(news_items)
        ]
    except Exception:
        ranked_items = []

    seen_urls = set()
    deduped_ranked_items = []

    for item in ranked_items:
        normalized_url = _normalize_url(item.get("url"))

        if normalized_url and normalized_url in seen_urls:
            continue

        if normalized_url:
            seen_urls.add(normalized_url)
        deduped_ranked_items.append(item)

    fallback_items = []

    for item in news_items:
        normalized_url = _normalize_url(item.get("url"))

        if normalized_url and normalized_url in seen_urls:
            continue

        if normalized_url:
            seen_urls.add(normalized_url)
        fallback_items.append(item)

    unique_ranked = await _select_unique_ranked_news(
        env,
        [*deduped_ranked_items, *fallback_items],
        limit,
    )

    return _apply_rank(unique_ranked, "N", limit)


async def are_news_duplicates_with_nim(
    env,
    left,
    right,
    confidence_threshold=0.75,
):
    nim_api_key = getattr(env, "NVIDIA_NIM_API_KEY", None)

    if not nim_api_key:
        return False

    try:
        content = await _chat_completion(
            nim_api_key,
            _chat_payload([
                {
                    "role": "system",
                    "content": (
                        "You identify duplicate AI news items. "
                        "Return only valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": _duplicate_check_prompt(left, right),
                },
            ], max_tokens=120),
            timeout_seconds=_nim_timeout_seconds(env),
        )
        parsed = json.loads(content)
        return (
            bool(parsed.get("duplicate"))
            and float(parsed.get("confidence", 0.0)) >= confidence_threshold
        )
    except Exception:
        return False


async def add_nim_summaries(env, digest):
    nim_api_key = getattr(env, "NVIDIA_NIM_API_KEY", None)
    timeout_seconds = _nim_timeout_seconds(env)

    papers = digest.get("papers", [])
    news_items = digest.get("news", [])
    items = [*papers, *news_items]

    if not nim_api_key:
        for item in papers:
            item["summary"] = _fallback_summary(item)
            item["summary_source"] = "fallback-no-nim"
            item["summary_generation_failed"] = False

        for item in news_items:
            item["summary"] = _fallback_summary(item)
            item["summary_source"] = "fallback-no-nim"
            item["summary_generation_failed"] = False

        return digest

    summary_semaphore = asyncio.Semaphore(_nim_summary_concurrency(env))

    async def _populate_summary(item):
        try:
            async with summary_semaphore:
                item["summary"] = await _summarize_item(
                    nim_api_key,
                    item,
                    timeout_seconds=timeout_seconds,
                )
            item["summary_source"] = "nim"
            item["summary_generation_failed"] = False
        except Exception:
            item["summary"] = _fallback_summary(item)
            item["summary_source"] = "fallback-error"
            item["summary_generation_failed"] = False

    await asyncio.gather(*(_populate_summary(item) for item in items))

    return digest


def finalize_news_for_publish(digest, similarity_threshold=SUMMARY_SIMILARITY_THRESHOLD):
    unique_news = []
    skipped_invalid = []
    skipped_duplicate = []

    for item in digest.get("news", []):
        if not is_publishable_news_item(item):
            item["publish_skip_reason"] = "invalid_summary"
            skipped_invalid.append(item)
            continue

        duplicate_item = next(
            (
                existing
                for existing in unique_news
                if summary_similarity(item.get("summary", ""), existing.get("summary", ""))
                >= similarity_threshold
            ),
            None,
        )

        if duplicate_item:
            item["publish_skip_reason"] = "duplicate_summary"
            item["duplicate_of_rank"] = duplicate_item.get("rank")
            skipped_duplicate.append(item)
            continue

        unique_news.append(item)

    occupied_ranks = set(digest.get("existing_news_ranks", []))
    available_ranks = [
        f"N{index}"
        for index in range(1, 11)
        if f"N{index}" not in occupied_ranks
    ]
    unique_news = unique_news[:len(available_ranks)]

    for item, rank in zip(unique_news, available_ranks):
        item["rank"] = rank

    digest["news"] = unique_news
    stats = dict(digest.get("stats", {}))
    stats["news_skipped_invalid_summary"] = len(skipped_invalid)
    stats["news_skipped_duplicate_summary"] = len(skipped_duplicate)
    stats["news_ready_for_publish"] = len(unique_news)
    digest["stats"] = stats
    digest["skipped_news"] = skipped_invalid + skipped_duplicate

    return digest
