from urllib.parse import urlencode

from http_client import fetch, response_json

from filtering import is_ai_related_text


HN_SEARCH_API_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_SEARCH_PAGE_SIZE = 1000


async def _fetch_json(url):
    response = await fetch(url)

    if not response.ok:
        raise RuntimeError(f"Hacker News search failed with status {response.status}")

    return await response_json(response)


def _normalize_story(story):
    story_id = story.get("objectID") or story.get("story_id")
    title = story.get("title", "")
    url = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"

    return {
        "title": title,
        "url": url,
        "source": "hacker news",
        "type": "news",
        "published_at": story.get("created_at_i"),
        "score_hint": story.get("points") or 0,
        "comments": story.get("num_comments") or 0,
        "summary_input": title,
    }


async def fetch_hackernews_ai_stories(
    limit=10,
    scan_count=1000,
    window_start_utc=None,
    window_end_utc=None,
):
    ai_stories = []
    page = 0
    scanned = 0

    while scanned < scan_count:
        page_size = min(HN_SEARCH_PAGE_SIZE, scan_count - scanned)
        query_params = {
            "tags": "story",
            "hitsPerPage": page_size,
            "page": page,
        }

        if window_start_utc and window_end_utc:
            query_params["numericFilters"] = (
                f"created_at_i>={int(window_start_utc.timestamp())},"
                f"created_at_i<{int(window_end_utc.timestamp())}"
            )

        data = await _fetch_json(f"{HN_SEARCH_API_URL}?{urlencode(query_params)}")
        hits = data.get("hits") or []

        for story in hits:
            title = story.get("title", "")
            url = story.get("url", "")

            if is_ai_related_text(title, url):
                ai_stories.append(_normalize_story(story))

        scanned += len(hits)

        if len(ai_stories) >= limit or not hits or page + 1 >= data.get("nbPages", 0):
            break

        page += 1

    return sorted(
        ai_stories,
        key=lambda item: item.get("score_hint", 0),
        reverse=True,
    )[:limit]
