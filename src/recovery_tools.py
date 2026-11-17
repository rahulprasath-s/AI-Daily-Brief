"""Bounded, public, credential-free retrieval tools for article recovery."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from article_content import MAX_DOWNLOAD_BYTES, SourceUnavailable, checked_content, extract_html, public_url


# Text-reader relay is limited to explicitly public publishers. It is never
# offered for a declared paywall, arbitrary search candidate, or unknown host.
PUBLIC_READER_HOSTS = {"openai.com", "www.openai.com", "anthropic.com", "www.anthropic.com", "blog.google", "blogs.nvidia.com"}


async def check_destination(url):
    public_url(url)
    parts = urlsplit(url)
    if parts.port not in (None, 80, 443):
        raise SourceUnavailable("Nonstandard source ports are not supported.")
    addresses = await asyncio.wait_for(asyncio.to_thread(
        socket.getaddrinfo, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    ), timeout=5)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise SourceUnavailable("Source resolves to a non-public network address.")


class RecoveryTools:
    def __init__(self, http):
        # A dedicated client must have no Notion/NIM credentials or cookies.
        self.http = http

    async def _download(self, url):
        for _ in range(5):
            await check_destination(url)
            async with self.http.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if not response.headers.get("location"):
                        raise SourceUnavailable("Recovery redirect has no destination.")
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise SourceUnavailable(f"Recovery source returned HTTP {response.status_code}.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise SourceUnavailable("Recovery response exceeds the download budget.")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace"), str(response.url), response.headers
        raise SourceUnavailable("Recovery source has too many redirects.")

    async def _metasearch(self, query):
        from ddgs import DDGS

        def retrieve():
            return DDGS(timeout=8).text(query[:240], max_results=6, backend="brave,duckduckgo")

        try:
            return await asyncio.wait_for(asyncio.to_thread(retrieve), timeout=25)
        except Exception:
            return []

    async def search(self, query):
        # Search returns candidates only. Snippets never become summary content.
        found = await self._metasearch(query)
        results = []
        for item in found:
            url = item.get("href") or item.get("url") or ""
            try:
                public_url(url)
            except SourceUnavailable:
                continue
            results.append({"url": url, "title": str(item.get("title", ""))[:300]})
        if results:
            return results[:6]
        text, _, _ = await self._download("https://www.bing.com/search?format=rss&q=" + quote(query[:240]))
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise SourceUnavailable("Public search returned an unsupported response.") from exc
        for item in root.findall("./channel/item"):
            url = item.findtext("link") or ""
            try:
                public_url(url)
            except SourceUnavailable:
                continue
            results.append({"url": url, "title": (item.findtext("title") or "")[:300]})
        return results[:6]

    async def fetch(self, url):
        text, final_url, headers = await self._download(url)
        if "html" not in headers.get("content-type", "").lower():
            raise SourceUnavailable("Recovery candidate is not a supported HTML article.")
        return extract_html(text, final_url)

    async def reader(self, url):
        if urlsplit(url).hostname not in PUBLIC_READER_HOSTS:
            raise SourceUnavailable("Public reader is not enabled for this publisher.")
        await check_destination(url)
        text, _, _ = await self._download("https://r.jina.ai/" + url)
        source = re.search(r"(?m)^URL Source:\s*(\S+)", text)
        if not source or source.group(1).rstrip("/") != url.rstrip("/"):
            raise SourceUnavailable("Public reader source identity could not be verified.")
        if "Markdown Content:" not in text:
            raise SourceUnavailable("Public reader returned no source body.")
        body = text.split("Markdown Content:", 1)[1].strip()
        if re.search(r"verify you are human|access denied|subscribe to continue|sign in to continue|content (?:was |is )?truncated|target url returned error", text, re.I):
            raise SourceUnavailable("Public reader returned restricted or incomplete content.")
        return checked_content(body, "public_reader_article", url)

    async def browser(self, url):
        from playwright.async_api import async_playwright

        await check_destination(url)
        async with asyncio.timeout(45), async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                visited, requests = set(), 0

                async def guard(route):
                    nonlocal requests
                    requests += 1
                    request = route.request
                    if requests > 80 or request.resource_type in {"image", "media", "font"} or request.method not in {"GET", "HEAD"}:
                        await route.abort()
                        return
                    try:
                        origin = urlsplit(request.url)
                        key = (origin.scheme, origin.netloc)
                        if key not in visited:
                            await check_destination(request.url)
                            visited.add(key)
                    except Exception:
                        await route.abort()
                        return
                    await route.continue_()

                await context.route("**/*", guard)
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                if not response or response.status >= 400:
                    raise SourceUnavailable(f"Browser source inaccessible (HTTP {response.status if response else 'unknown'}).")
                # Wait briefly for article rendering; do not interact with access challenges.
                try:
                    await page.locator("article, main").first.wait_for(timeout=3000)
                except Exception:
                    pass
                await check_destination(page.url)
                # Refuse oversized DOMs rather than sending partial article text.
                size = await page.evaluate("new TextEncoder().encode(document.documentElement.outerHTML).length")
                if size > MAX_DOWNLOAD_BYTES:
                    raise SourceUnavailable("Rendered source exceeds the download budget.")
                return extract_html(await page.content(), page.url)
            finally:
                await browser.close()
