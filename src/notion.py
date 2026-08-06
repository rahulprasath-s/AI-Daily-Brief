import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from http_client import fetch, response_json

try:
    from nim import is_publishable_news_item, summary_similarity
except ModuleNotFoundError:
    from src.nim import is_publishable_news_item, summary_similarity


NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_DATABASES_URL = "https://api.notion.com/v1/databases"
NOTION_VERSION = "2022-06-28"
DEFAULT_STATUS = "NOT YET"


def _truncate(value, max_length=1900):
    text = "" if value is None else str(value)
    return text[:max_length]


def _normalize_title(value):
    return " ".join(str(value or "").split()).strip().lower()


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


def item_identity(item):
    identities = item_identities(item)

    for identity_kind in ("url", "title"):
        for identity in identities:
            if identity[0] == identity_kind:
                return identity

    return None


def item_identities(item):
    identities = set()
    normalized_url = _normalize_url(item.get("url"))

    if normalized_url:
        identities.add(("url", normalized_url))

    normalized_title = _normalize_title(item.get("title"))

    if normalized_title:
        identities.add(("title", normalized_title))

    return identities


def _short_description(item, max_words=100):
    text = item.get("description") or item.get("summary") or item.get("summary_input", "")
    words = " ".join(str(text).split()).split(" ")

    if len(words) <= max_words:
        return " ".join(words)

    return " ".join(words[:max_words]).rstrip(".,;:") + "..."


def _page_description(page):
    description_property = page.get("properties", {}).get("Description", {})
    rich_text = description_property.get("rich_text") or []

    if not rich_text:
        return ""

    return " ".join(
        text_item.get("plain_text", "")
        for text_item in rich_text
        if text_item.get("plain_text")
    ).strip()


def _source_name(item):
    source = item.get("source", "")
    source_map = {
        "arxiv": "arXiv",
        "arXiv": "arXiv",
        "hacker news": "hacker news",
        "hugging face": "hugging face",
        "techmeme": "techmeme",
    }
    return source_map.get(source, source)


def _notion_properties(database_id, digest_date, item):
    return {
        "parent": {"database_id": database_id},
        "properties": {
            "Date": {
                "date": {"start": digest_date},
            },
            "Type": {
                "select": {"name": item.get("type", "news")},
            },
            "Title": {
                "title": [
                    {
                        "text": {
                            "content": _truncate(item.get("title", "Untitled"), 200),
                        },
                    },
                ],
            },
            "Description": {
                "rich_text": [
                    {
                        "text": {
                            "content": _truncate(_short_description(item)),
                        },
                    },
                ],
            },
            "Source": {
                "select": {"name": _source_name(item)},
            },
            "Status": {
                "status": {"name": DEFAULT_STATUS},
            },
            "Link": {
                "url": item.get("url"),
            },
            "Rank": {
                "rich_text": [
                    {
                        "text": {
                            "content": item.get("rank", ""),
                        },
                    },
                ],
            },
        },
    }


async def _create_page(notion_token, payload):
    response = await fetch(
        NOTION_PAGES_URL,
        method="POST",
        headers={
            "authorization": f"Bearer {notion_token}",
            "content-type": "application/json",
            "notion-version": NOTION_VERSION,
        },
        body=json.dumps(payload),
    )

    data = await response_json(response)

    if not response.ok:
        raise RuntimeError(f"Notion page create failed: {data}")

    return data


def _rank_from_page(page):
    rank_property = page.get("properties", {}).get("Rank", {})
    rich_text = rank_property.get("rich_text") or []

    if not rich_text:
        return ""

    return rich_text[0].get("plain_text", "")


def _link_from_page(page):
    return page.get("properties", {}).get("Link", {}).get("url", "") or ""


async def _query_existing_pages_for_date(notion_token, database_id, digest_date):
    response = await fetch(
        f"{NOTION_DATABASES_URL}/{database_id}/query",
        method="POST",
        headers={
            "authorization": f"Bearer {notion_token}",
            "content-type": "application/json",
            "notion-version": NOTION_VERSION,
        },
        body=json.dumps({
            "page_size": 100,
            "filter": {
                "property": "Date",
                "date": {
                    "equals": digest_date,
                },
            },
        }),
    )

    data = await response_json(response)

    if not response.ok:
        raise RuntimeError(f"Notion database query failed: {data}")

    return {
        _rank_from_page(page): page
        for page in data.get("results", [])
        if _rank_from_page(page)
    }


async def _query_recent_pages(notion_token, database_id, page_size=100, max_pages=None):
    results = []
    start_cursor = None
    pages_fetched = 0

    while True:
        payload = {
            "page_size": page_size,
            "sorts": [
                {
                    "property": "Date",
                    "direction": "descending",
                },
            ],
        }

        if start_cursor:
            payload["start_cursor"] = start_cursor

        response = await fetch(
            f"{NOTION_DATABASES_URL}/{database_id}/query",
            method="POST",
            headers={
                "authorization": f"Bearer {notion_token}",
                "content-type": "application/json",
                "notion-version": NOTION_VERSION,
            },
            body=json.dumps(payload),
        )

        data = await response_json(response)

        if not response.ok:
            raise RuntimeError(f"Notion database query failed: {data}")

        results.extend(data.get("results", []))
        pages_fetched += 1

        if not data.get("has_more") or not data.get("next_cursor"):
            break

        if max_pages is not None and pages_fetched >= max_pages:
            break

        start_cursor = data.get("next_cursor")

    return results


def _page_title(page):
    title_property = page.get("properties", {}).get("Title", {})
    title = title_property.get("title") or []

    if not title:
        return ""

    return title[0].get("plain_text", "")


def _existing_item_identities(pages):
    identities = set()

    for page in pages:
        identities.update(item_identities({
            "url": _link_from_page(page),
            "title": _page_title(page),
        }))

    return identities


def _page_type(page):
    type_property = page.get("properties", {}).get("Type", {})
    return (type_property.get("select") or {}).get("name", "")


def _page_date(page):
    date_property = page.get("properties", {}).get("Date", {})
    return (date_property.get("date") or {}).get("start", "")


def _page_as_item(page):
    description = _page_description(page)
    return {
        "id": page.get("id"),
        "date": _page_date(page),
        "rank": _rank_from_page(page),
        "title": _page_title(page),
        "url": _link_from_page(page),
        "type": _page_type(page),
        "summary": description,
        "summary_input": description,
    }


async def get_published_items(env, page_size=100, max_pages=None):
    notion_token = getattr(env, "NOTION_TOKEN", None)
    database_id = getattr(env, "NOTION_DATABASE_ID", None)

    if not notion_token or not database_id:
        return []

    pages = await _query_recent_pages(
        notion_token,
        database_id,
        page_size=page_size,
        max_pages=max_pages,
    )
    return [_page_as_item(page) for page in pages]


def _published_items_by_rank_for_date(published_items, digest_date):
    return {
        item.get("rank", ""): item
        for item in published_items
        if item.get("date") == digest_date and item.get("rank")
    }


async def publish_digest_to_notion(env, digest, published_items=None):
    notion_token = getattr(env, "NOTION_TOKEN", None)
    database_id = getattr(env, "NOTION_DATABASE_ID", None)

    if not notion_token or not database_id:
        raise RuntimeError("Missing NOTION_TOKEN or NOTION_DATABASE_ID")

    items = [*digest.get("papers", []), *digest.get("news", [])]
    if published_items is None:
        existing_pages_by_rank = await _query_existing_pages_for_date(
            notion_token,
            database_id,
            digest["date"],
        )
        published_items = await get_published_items(env)
    else:
        existing_pages_by_rank = _published_items_by_rank_for_date(
            published_items,
            digest["date"],
        )
    published_identities = set().union(*(
        item_identities(item) for item in published_items
    )) if published_items else set()
    published_news_summaries = [
        item.get("summary", "")
        for item in published_items
        if item.get("type") == "news" and item.get("summary")
    ]
    created_pages = []
    skipped_pages = []

    for item in items:
        existing_page = existing_pages_by_rank.get(item.get("rank", ""))

        if existing_page:
            skipped_pages.append({
                "id": existing_page.get("id"),
                "rank": item.get("rank"),
                "title": (
                    existing_page.get("title")
                    or _page_title(existing_page)
                    or item.get("title")
                ),
                "reason": "duplicate_date_rank",
            })
            continue

        identities = item_identities(item)
        if identities & published_identities:
            skipped_pages.append({
                "rank": item.get("rank"),
                "title": item.get("title"),
                "reason": "duplicate_history_identity",
            })
            continue

        if item.get("type") == "news":
            if not is_publishable_news_item(item):
                skipped_pages.append({
                    "rank": item.get("rank"),
                    "title": item.get("title"),
                    "reason": "invalid_summary",
                })
                continue

            if any(
                summary_similarity(item.get("summary", ""), existing_summary) >= 0.7
                for existing_summary in published_news_summaries
                if existing_summary
            ):
                skipped_pages.append({
                    "rank": item.get("rank"),
                    "title": item.get("title"),
                    "reason": "duplicate_history_summary",
                })
                continue

        payload = _notion_properties(database_id, digest["date"], item)
        created = await _create_page(notion_token, payload)
        created_pages.append({
            "id": created.get("id"),
            "rank": item.get("rank"),
            "title": item.get("title"),
        })
        published_identities.update(identities)
        if item.get("type") == "news":
            published_news_summaries.append(item.get("summary", ""))

    return {
        "created": len(created_pages),
        "skipped": len(skipped_pages),
        "pages": created_pages,
        "skipped_pages": skipped_pages,
    }


async def filter_missing_digest_items(env, digest, published_items=None):
    notion_token = getattr(env, "NOTION_TOKEN", None)
    database_id = getattr(env, "NOTION_DATABASE_ID", None)

    if not notion_token or not database_id:
        raise RuntimeError("Missing NOTION_TOKEN or NOTION_DATABASE_ID")

    if published_items is None:
        existing_pages_by_rank = await _query_existing_pages_for_date(
            notion_token,
            database_id,
            digest["date"],
        )
        existing_identities = _existing_item_identities(
            existing_pages_by_rank.values()
        )
    else:
        existing_pages_by_rank = _published_items_by_rank_for_date(
            published_items,
            digest["date"],
        )
        existing_identities = set().union(*(
            item_identities(item) for item in existing_pages_by_rank.values()
        )) if existing_pages_by_rank else set()

    missing_papers = [
        item for item in digest.get("papers", [])
        if (
            item.get("rank", "") not in existing_pages_by_rank
            and not (item_identities(item) & existing_identities)
        )
    ]
    missing_news = [
        item for item in digest.get("news", [])
        if not (item_identities(item) & existing_identities)
    ]
    existing_news_ranks = sorted(
        rank for rank in existing_pages_by_rank if rank.startswith("N")
    )

    return {
        **digest,
        "papers": missing_papers,
        "news": missing_news,
        "existing_count": len(existing_pages_by_rank),
        "existing_same_day_identity_count": len(existing_identities),
        "existing_news_ranks": existing_news_ranks,
    }


async def get_recent_item_identities(env, page_size=100, max_pages=None):
    published_items = await get_published_items(
        env,
        page_size=page_size,
        max_pages=max_pages,
    )
    identities = set()

    for item in published_items:
        identities.update(item_identities(item))

    return identities
