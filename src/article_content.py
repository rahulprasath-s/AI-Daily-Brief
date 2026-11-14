"""Source extraction for the optional CPython summary stage; no model calls."""

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import trafilatura


MAX_DOWNLOAD_BYTES = 3_000_000
MAX_SOURCE_CHARS = 120_000


class SourceUnavailable(RuntimeError):
    """No complete, supported source content is accessible."""


@dataclass(frozen=True)
class ArticleContent:
    text: str
    kind: str
    url: str

    @property
    def fingerprint(self):
        value = json.dumps([self.url, self.kind, self.text], ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise SourceUnavailable("Unsupported source URL.")
    if parts.hostname.lower() == "localhost" or parts.hostname.lower().endswith(".local"):
        raise SourceUnavailable("Local addresses are not supported sources.")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return url
    if not address.is_global:
        raise SourceUnavailable("Private addresses are not supported sources.")
    return url


async def download(client, url):
    for _ in range(6):
        public_url(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                if not response.headers.get("location"):
                    raise SourceUnavailable("Source redirect has no destination.")
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code in (401, 402, 403, 404, 410, 451):
                raise SourceUnavailable(f"Source inaccessible (HTTP {response.status_code}); no headline fallback.")
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise SourceUnavailable("Source exceeds download budget; no partial summary generated.")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace"), str(response.url), response.headers
    raise SourceUnavailable("Too many source redirects.")


def checked_content(text, kind, url, minimum_words=40):
    text = text.strip() if text else ""
    if len(text.split()) < minimum_words:
        raise SourceUnavailable("Insufficient substantive source content; no headline-only summary generated.")
    if len(text) > MAX_SOURCE_CHARS:
        raise SourceUnavailable("Full source exceeds the supported length budget; no partial summary generated.")
    return ArticleContent(text, kind, url)


def extract_html(html, url):
    # Respect declared paywalls rather than summarizing a preview as a full article.
    if re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', html, re.I):
        raise SourceUnavailable("Publisher declares this content paywalled; full article unavailable.")
    text = trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True,
        include_links=False, favor_precision=True,
    )
    if text and len(text.split()) < 150 and re.search(
        r"verify you are human|enable javascript and cookies|access denied|subscribe to continue|sign in to continue",
        text, re.I,
    ):
        raise SourceUnavailable("Access challenge or subscription screen; article unavailable.")
    return checked_content(text, "html_article", url)


async def youtube_content(url):
    parts = urlsplit(url)
    if parts.hostname in ("youtu.be", "www.youtu.be"):
        video_id = parts.path.strip("/")
    elif parts.path.startswith(("/shorts/", "/embed/")):
        video_id = parts.path.split("/")[2]
    else:
        video_id = parse_qs(parts.query).get("v", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise SourceUnavailable("Unsupported YouTube video URL.")

    def retrieve():
        from requests import Session
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

        class BoundedSession(Session):
            def request(self, *args, **kwargs):
                kwargs.setdefault("timeout", 15)
                return super().request(*args, **kwargs)

        with BoundedSession() as session:
            api = YouTubeTranscriptApi(http_client=session)
            try:
                transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
            except NoTranscriptFound:
                available = list(api.list(video_id))
                if not available:
                    raise SourceUnavailable("No YouTube transcript is available.")
                transcript = available[0].fetch()
            return " ".join(segment.text for segment in transcript)

    try:
        text = await asyncio.wait_for(asyncio.to_thread(retrieve), timeout=60)
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable(f"YouTube transcript unavailable ({type(exc).__name__}); video title was not summarized.") from exc
    return checked_content(text, "youtube_transcript", url)


async def extract_source(client, url):
    public_url(url)
    parts = urlsplit(url)
    host = parts.hostname.lower()
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"):
        return await youtube_content(url)

    if host == "huggingface.co":
        segments = parts.path.strip("/").split("/")
        if len(segments) != 2 or segments[0] in ("spaces", "datasets"):
            raise SourceUnavailable("Only Hugging Face model-card URLs are supported by this adapter.")
        card_url = f"https://huggingface.co/{segments[0]}/{segments[1]}/raw/main/README.md"
        text, _, _ = await download(client, card_url)
        # YAML metadata alone is not a model-card narrative.
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
        return checked_content(text, "huggingface_model_card", card_url)

    if host.startswith("status.") and re.fullmatch(r"/incidents/[A-Za-z0-9_-]+/?", parts.path):
        incident_id = parts.path.strip("/").split("/")[-1]
        api_url = f"{parts.scheme}://{parts.netloc}/api/v2/incidents/{incident_id}.json"
        try:
            raw, _, _ = await download(client, api_url)
            incident = json.loads(raw)
            if "incident" in incident:
                incident = incident["incident"]
            updates = incident.get("incident_updates") or []
            text = "\n\n".join([
                f"Incident: {incident.get('name', '')}. Current status: {incident.get('status', '')}.",
                *[f"{entry.get('created_at', '')} ({entry.get('status', '')}): {entry.get('body', '')}" for entry in updates],
            ])
            if not updates:
                raise SourceUnavailable("No incident update content is available.")
            return checked_content(text, "status_incident", api_url, minimum_words=15)
        except (SourceUnavailable, json.JSONDecodeError):
            # A custom status page may not expose the Statuspage JSON format.
            pass

    html, final_url, headers = await download(client, url)
    if "html" not in headers.get("content-type", "").lower():
        raise SourceUnavailable("Unsupported response format; HTML article content was expected.")
    return extract_html(html, final_url)
