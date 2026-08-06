from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from http_client import fetch

from filtering import is_ai_related_text


TECHMEME_FEED_URL = "https://www.techmeme.com/feed.xml"


def _text(element, name):
    child = element.find(name)
    return "" if child is None or child.text is None else " ".join(child.text.split())


def _parse_pub_date(value):
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None

    return parsed.astimezone(timezone.utc).isoformat()


def _parse_techmeme_feed(xml_text):
    root = ET.fromstring(xml_text)
    items = []

    for entry in root.findall("./channel/item"):
        title = _text(entry, "title")
        url = _text(entry, "link")
        published_at = _parse_pub_date(_text(entry, "pubDate"))

        if not title or not url or not published_at:
            continue

        if not is_ai_related_text(title, url):
            continue

        items.append({
            "title": title,
            "url": url,
            "source": "techmeme",
            "type": "news",
            "published_at": published_at,
            "score_hint": 0,
            "summary_input": title,
        })

    return items


async def fetch_techmeme_ai_stories(limit=10):
    response = await fetch(TECHMEME_FEED_URL)

    if not response.ok:
        raise RuntimeError(f"Techmeme feed request failed with status {response.status}")

    xml_text = await response.text()
    return _parse_techmeme_feed(xml_text)[:limit]
