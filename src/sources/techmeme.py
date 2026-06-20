from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from js import fetch

from filtering import is_ai_related_text


TECHMEME_URL = "https://www.techmeme.com/"
TECHMEME_SNAPSHOT_PATTERN = re.compile(
    r"([A-Z][a-z]+ \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M)"
)


class TechmemeLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._current_href = None
        self._current_class = ""
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return

        attr_map = dict(attrs)
        self._current_href = attr_map.get("href")
        self._current_class = attr_map.get("class", "")
        self._text_parts = []

    def handle_data(self, data):
        if self._current_href:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._current_href:
            return

        title = " ".join(" ".join(self._text_parts).split())

        if title:
            self.links.append({
                "title": title,
                "href": self._current_href,
                "class": self._current_class,
            })

        self._current_href = None
        self._current_class = ""
        self._text_parts = []


def _is_story_link(link):
    href = link["href"]

    if href.startswith("#") or href.startswith("javascript:"):
        return False

    if len(link["title"]) < 12:
        return False

    # Techmeme navigation links are useful to ignore before keyword filtering.
    noisy_titles = {"about", "archive", "events", "login", "sponsor posts", "tip us"}
    return link["title"].strip().lower() not in noisy_titles


def _normalize_link(link, snapshot_timestamp=None):
    url = urljoin(TECHMEME_URL, link["href"])
    title = link["title"]

    return {
        "title": title,
        "url": url,
        "source": "techmeme",
        "type": "news",
        "published_at": snapshot_timestamp,
        "score_hint": 0,
        "summary_input": title,
    }


def _snapshot_timestamp(html):
    match = TECHMEME_SNAPSHOT_PATTERN.search(html)

    if not match:
        return None

    parsed = datetime.strptime(match.group(1), "%B %d, %Y, %I:%M %p")
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _dedupe_items(items):
    seen = set()
    unique_items = []

    for item in items:
        key = (item["title"].lower(), item["url"])

        if key in seen:
            continue

        seen.add(key)
        unique_items.append(item)

    return unique_items


async def fetch_techmeme_ai_stories(limit=10):
    response = await fetch(TECHMEME_URL)

    if not response.ok:
        raise RuntimeError(f"Techmeme request failed with status {response.status}")

    html = await response.text()
    snapshot_timestamp = _snapshot_timestamp(html)
    parser = TechmemeLinkParser()
    parser.feed(html)

    items = [
        _normalize_link(link, snapshot_timestamp=snapshot_timestamp)
        for link in parser.links
        if _is_story_link(link) and is_ai_related_text(link["title"], link["href"])
    ]

    return _dedupe_items(items)[:limit]
