import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from remote_summary_cache import CacheUnavailable, RemoteSummaryCache, open_summary_cache


class RemoteCacheTests(unittest.TestCase):
    def test_fresh_runner_restores_generated_result_and_pending_write(self):
        stored = {}

        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            key = request.url.path.split("/cache/", 1)[1]
            if request.method == "PUT":
                stored[key] = json.loads(request.content)
                return httpx.Response(200, json={"saved": True})
            if request.method == "DELETE":
                stored.pop(key, None)
                return httpx.Response(200, json={"deleted": True})
            return httpx.Response(200, json={"value": stored[key]}) if key in stored else httpx.Response(404, json={"found": False})

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            cache = RemoteSummaryCache(first, "https://cache.example", "test-token", client=httpx.Client(transport=httpx.MockTransport(handler)))
            cache.set("final", {"text": "Saved model result"})
            cache.set("pending_write:page", {"properties": {"Summary": "Saved model result"}})
            cache.close()
            cache = RemoteSummaryCache(second, "https://cache.example", "test-token", client=httpx.Client(transport=httpx.MockTransport(handler)))
            self.assertEqual(cache.get("final"), {"text": "Saved model result"})
            self.assertIsNotNone(cache.get("pending_write:page"))
            cache.delete("pending_write:page")
            self.assertIsNone(cache.get("pending_write:page"))
            cache.close()

    def test_cache_failure_is_never_a_miss(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CacheUnavailable):
                RemoteSummaryCache(directory, "https://cache.example", "test-token", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
        self.assertEqual(len(calls), 3)

    def test_invalid_credentials_are_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CacheUnavailable):
                RemoteSummaryCache(directory, "https://cache.example", "bad", client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(len(calls), 1)

    def test_daily_mode_refuses_local_only_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "persistent cache"):
                open_summary_cache(directory, SimpleNamespace(), require_remote=True)
