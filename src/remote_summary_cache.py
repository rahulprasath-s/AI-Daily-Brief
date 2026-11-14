"""Authenticated persistent checkpoints shared by ephemeral GitHub runners."""

import json
import time
from urllib.parse import quote, urlsplit

import httpx

from nim_article_summary import retry_after_seconds
from summary_cache import SummaryCache


class CacheUnavailable(RuntimeError):
    pass


class RemoteSummaryCache(SummaryCache):
    def __init__(self, directory, url, token, client=None, sleep=time.sleep):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("SUMMARY_CACHE_URL must be an HTTPS endpoint without credentials or query parameters.")
        if not token:
            raise ValueError("Missing SUMMARY_CACHE_TOKEN")
        super().__init__(directory)
        self.url, self.token, self.sleep = url.rstrip("/"), token, sleep
        self.http = client or httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False)
        try:
            self._request("GET", "__probe__")
        except Exception:
            self.close()
            raise

    def _request(self, method, key, value=None):
        for attempt in range(3):
            response = None
            try:
                response = self.http.request(
                    method, f"{self.url}/cache/{quote(key, safe='')}",
                    headers={"authorization": f"Bearer {self.token}", "content-type": "application/json"},
                    content=json.dumps(value, ensure_ascii=False).encode() if value is not None else None,
                )
                if method == "GET" and response.status_code == 404 and response.json() == {"found": False}:
                    return None
                if response.is_success:
                    body = response.json()
                    return body["value"] if method == "GET" else body
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise CacheUnavailable(f"Persistent cache rejected request (HTTP {response.status_code}); check configuration.")
            except (httpx.TimeoutException, httpx.TransportError):
                pass
            if attempt == 2:
                break
            delay = retry_after_seconds(response.headers.get("retry-after")) if response is not None else None
            if delay is not None and delay > 20:
                raise CacheUnavailable("Persistent cache requires a later retry; refusing to generate without durable checkpoints.")
            self.sleep(delay if delay is not None else 2 ** attempt)
        raise CacheUnavailable("Persistent cache temporarily unavailable; generation is paused to protect saved results.")

    def get(self, key):
        local = super().get(key)
        if local is not None:
            return local
        value = self._request("GET", key)
        if value is not None:
            super().set(key, value)
        return value

    def set(self, key, value):
        super().set(key, value)
        # This must succeed before callers can attempt a Notion write.
        self._request("PUT", key, value)

    def delete(self, key):
        self._request("DELETE", key)
        super().delete(key)

    def close(self):
        self.http.close()
        super().close()


def open_summary_cache(directory, env, require_remote=False):
    url = getattr(env, "SUMMARY_CACHE_URL", None)
    token = getattr(env, "SUMMARY_CACHE_TOKEN", None)
    if bool(url) != bool(token):
        raise ValueError("Both SUMMARY_CACHE_URL and SUMMARY_CACHE_TOKEN are required for the persistent cache.")
    if url and token:
        return RemoteSummaryCache(directory, url, token)
    if require_remote:
        raise ValueError("Daily enrichment requires a configured persistent cache; local-only mode is refused.")
    return SummaryCache(directory)
