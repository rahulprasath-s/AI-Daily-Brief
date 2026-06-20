import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from workers import fetch


NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_DATABASES_URL = "https://api.notion.com/v1/databases"
NOTION_VERSION = "2022-06-28"


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


def _item_identity(item):
    normalized_url = _normalize_url(item.get("url"))

    if normalized_url:
        return ("url", normalized_url)

    normalized_title = _normalize_title(item.get("title"))

    if normalized_title:
        return ("title", normalized_title)

    return None


def _short_description(item, max_words=100):
    text = item.get("description") or item.get("summary") or item.get("summary_input", "")
    words = " ".join(str(text).split()).split(" ")

    if len(words) <= max_words:
        return " ".join(words)

    return " ".join(words[:max_words]).rstrip(".,;:") + "..."


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

    data = await response.json()

    if not response.ok:
        raise RuntimeError(f"Notion page create failed: {data}")

    return data


def _rank_from_page(page):
    rank_property = page.get("properties", {}).get("Rank", {})
    rich_text = rank_property.get("rich_text", [])

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

    data = await response.json()

    if not response.ok:
        raise RuntimeError(f"Notion database query failed: {data}")

    return {
        _rank_from_page(page): page
        for page in data.get("results", [])
        if _rank_from_page(page)
    }


async def _query_recent_pages(notion_token, database_id, page_size=100, max_pages=5):
    results = []
    start_cursor = None

    for _ in range(max_pages):
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

        data = await response.json()

        if not response.ok:
            raise RuntimeError(f"Notion database query failed: {data}")

        results.extend(data.get("results", []))

        if not data.get("has_more") or not data.get("next_cursor"):
            break

        start_cursor = data.get("next_cursor")

    return results


def _page_title(page):
    title_property = page.get("properties", {}).get("Title", {})
    title = title_property.get("title", [])

    if not title:
        return ""

    return title[0].get("plain_text", "")


def _existing_item_identities(pages):
    identities = set()

    for page in pages:
        normalized_url = _normalize_url(_link_from_page(page))

        if normalized_url:
            identities.add(("url", normalized_url))

        normalized_title = _normalize_title(_page_title(page))

        if normalized_title:
            identities.add(("title", normalized_title))

    return identities


async def publish_digest_to_notion(env, digest):
    notion_token = getattr(env, "NOTION_TOKEN", None)
    database_id = getattr(env, "NOTION_DATABASE_ID", None)

    if not notion_token or not database_id:
        raise RuntimeError("Missing NOTION_TOKEN or NOTION_DATABASE_ID")

    items = [*digest.get("papers", []), *digest.get("news", [])]
    existing_pages_by_rank = await _query_existing_pages_for_date(
        notion_token,
        database_id,
        digest["date"],
    )
    created_pages = []
    skipped_pages = []

    for item in items:
        existing_page = existing_pages_by_rank.get(item.get("rank", ""))

        if existing_page:
            skipped_pages.append({
                "id": existing_page.get("id"),
                "rank": item.get("rank"),
                "title": _page_title(existing_page) or item.get("title"),
            })
            continue

        payload = _notion_properties(database_id, digest["date"], item)
        created = await _create_page(notion_token, payload)
        created_pages.append({
            "id": created.get("id"),
            "rank": item.get("rank"),
            "title": item.get("title"),
        })

    return {
        "created": len(created_pages),
        "skipped": len(skipped_pages),
        "pages": created_pages,
        "skipped_pages": skipped_pages,
    }


async def filter_missing_digest_items(env, digest):
    notion_token = getattr(env, "NOTION_TOKEN", None)
    database_id = getattr(env, "NOTION_DATABASE_ID", None)

    if not notion_token or not database_id:
        raise RuntimeError("Missing NOTION_TOKEN or NOTION_DATABASE_ID")

    existing_pages_by_rank = await _query_existing_pages_for_date(
        notion_token,
        database_id,
        digest["date"],
    )
    recent_pages = await _query_recent_pages(notion_token, database_id)
    existing_identities = _existing_item_identities(recent_pages)

    missing_papers = [
        item for item in digest.get("papers", [])
        if (
            item.get("rank", "") not in existing_pages_by_rank
            and _item_identity(item) not in existing_identities
        )
    ]
    missing_news = [
        item for item in digest.get("news", [])
        if (
            item.get("rank", "") not in existing_pages_by_rank
            and _item_identity(item) not in existing_identities
        )
    ]

    return {
        **digest,
        "papers": missing_papers,
        "news": missing_news,
        "existing_count": len(existing_pages_by_rank),
        "existing_recent_count": len(recent_pages),
    }
