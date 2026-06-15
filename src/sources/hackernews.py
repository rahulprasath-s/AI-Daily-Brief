from js import fetch

from filtering import is_ai_related_text


HN_API_BASE = "https://hacker-news.firebaseio.com/v0"


async def _fetch_json(url):
    response = await fetch(url)

    if not response.ok:
        raise RuntimeError(f"Hacker News request failed with status {response.status}")

    return (await response.json()).to_py()


def _normalize_story(story):
    title = story.get("title", "")
    url = story.get("url") or f"https://news.ycombinator.com/item?id={story.get('id')}"

    return {
        "title": title,
        "url": url,
        "source": "hacker news",
        "type": "news",
        "published_at": story.get("time"),
        "score_hint": story.get("score", 0),
        "comments": story.get("descendants", 0),
        "summary_input": title,
    }


async def fetch_hackernews_ai_stories(limit=10, scan_count=80):
    story_ids = await _fetch_json(f"{HN_API_BASE}/topstories.json")
    stories = []

    for story_id in story_ids[:scan_count]:
        story = await _fetch_json(f"{HN_API_BASE}/item/{story_id}.json")

        if not story or story.get("type") != "story":
            continue

        title = story.get("title", "")
        url = story.get("url", "")

        if is_ai_related_text(title, url):
            stories.append(_normalize_story(story))

        if len(stories) >= limit:
            break

    return stories
