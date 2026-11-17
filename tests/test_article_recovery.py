import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from article_content import ArticleContent, SourceUnavailable
from article_recovery import RecoveryAgent, build_recovery_graph
from nim_article_summary import source_attribution
from recovery_tools import RecoveryTools, check_destination
from remote_summary_cache import CacheUnavailable
from summary_cache import SummaryCache

TEXT = (
    "The company announced an independent assessment program with four external research teams. "
    "The assessors will examine model safeguards and report their findings. "
    "The company says the work is intended to improve transparency, but the announcement "
    "does not establish that the safeguards are effective. Access is limited to agreed systems."
)
QUOTE = "The company announced an independent assessment program with four external research teams."


def response(tool=None, index=None, match=None, quote=QUOTE):
    if match is not None:
        return json.dumps({"match": match, "relation": "same_article" if match else "unrelated", "evidence_quote": quote})
    return json.dumps({"tool": tool, **({"index": index} if index is not None else {})})


class RecoveryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = SummaryCache(self.directory.name)
        self.original = "https://publisher.example/article"
        self.content = ArticleContent(TEXT, "html_article", self.original)
        self.tools = SimpleNamespace(reader=AsyncMock(return_value=self.content), browser=AsyncMock(return_value=self.content), search=AsyncMock(return_value=[]), fetch=AsyncMock(return_value=self.content))

    def tearDown(self):
        self.cache.close()
        self.directory.cleanup()

    def agent(self, answers, **kwargs):
        self.nim = SimpleNamespace(model="test", complete=AsyncMock(side_effect=answers))
        return RecoveryAgent(self.nim, self.cache, self.tools, **kwargs)

    async def recover(self, agent, failure="HTTP 403"):
        return await agent.recover("page", "Independent assessment program", self.original, "2026-11-17", failure)

    async def test_public_browser_content_is_verified_and_reused_without_more_calls(self):
        agent = self.agent([response("browser", -1), response(match=True)])
        content, trace = await self.recover(agent)
        self.assertEqual(content, self.content)
        self.assertEqual(trace[-1]["tool"], "verify")
        await self.recover(agent)
        self.assertEqual(self.nim.complete.await_count, 2)
        self.tools.browser.assert_awaited_once()

    async def test_reader_is_offered_only_for_allowlisted_public_publishers(self):
        self.original = "https://openai.com/index/example/"
        self.tools.reader.return_value = ArticleContent(TEXT, "public_reader_article", self.original)
        agent = self.agent([response("reader", -1), response(match=True)])
        content, _ = await self.recover(agent)
        self.assertEqual(content.kind, "public_reader_article")
        self.tools.reader.assert_awaited_once_with(self.original)
        choices = agent.available({"failure": "Publisher declares paywall", "used": [], "candidates": [], "original_url": self.original})
        self.assertFalse(any(choice["tool"] == "reader" for choice in choices))

    async def test_paywall_search_retrieves_real_alternative_and_preserves_provenance(self):
        alternative = "https://research.example/assessment"
        self.tools.search.return_value = [{"title": "Assessment program", "url": alternative}]
        self.tools.fetch.return_value = ArticleContent(TEXT, "html_article", alternative)
        agent = self.agent([response("search", 0), response("fetch", 0), response(match=True)])
        content, _ = await self.recover(agent, "Publisher declares this content paywalled")
        self.assertEqual(content.kind, "recovered_alternative")
        self.assertEqual(content.url, alternative)
        self.tools.browser.assert_not_awaited()
        attributed = source_attribution("The company announced assessments.", content.kind, content.url)
        self.assertIn("alternative source from research example", attributed)
        self.assertEqual(source_attribution(attributed, content.kind, content.url), attributed)

    async def test_search_snippets_never_become_source_text(self):
        self.tools.search.return_value = [{"title": "A matching headline", "url": "https://other.example/story"}]
        agent = self.agent([response("search", 0), response("stop")])
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent, "paywall")
        self.tools.fetch.assert_not_awaited()
        self.assertEqual(self.nim.complete.await_count, 2)

    async def test_hallucinated_evidence_is_rejected_even_when_model_says_match(self):
        agent = self.agent([response("browser", -1), response(match=True, quote="This quoted evidence does not exist anywhere in the retrieved source."), response("stop")])
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent)

    async def test_unrelated_content_is_rejected(self):
        agent = self.agent([response("browser", -1), response(match=False), response("stop")])
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent)

    async def test_invented_tools_and_urls_cannot_be_executed(self):
        agent = self.agent([json.dumps({"tool": "shell", "url": "http://localhost/private"}), response(match=True)])
        await self.recover(agent)
        self.tools.browser.assert_awaited_once_with(self.original)

    async def test_tool_budget_is_bounded_and_completed_failures_are_not_replayed(self):
        self.tools.browser.side_effect = SourceUnavailable("HTTP 403")
        agent = self.agent([response("browser", -1)], max_steps=1)
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent)
        with self.assertRaisesRegex(SourceUnavailable, "exhausted"):
            await self.recover(agent)
        self.tools.browser.assert_awaited_once()
        self.nim.complete.assert_awaited_once()

    async def test_failed_attempt_can_retry_after_cooldown(self):
        self.tools.browser.side_effect = SourceUnavailable("temporary block")
        agent = self.agent([response("browser", -1)], max_steps=1)
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent)
        saved = self.cache.get("recovery:page")
        saved["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.cache.set("recovery:page", saved)
        self.tools.browser.side_effect = None
        self.nim.complete = AsyncMock(return_value=response(match=True))
        content, _ = await self.recover(agent)
        self.assertEqual(content, self.content)
        self.assertEqual(self.tools.browser.await_count, 2)

    async def test_paywalled_search_candidate_is_never_rendered_to_bypass_access(self):
        self.tools.search.return_value = [{"title": "Match", "url": "https://paywall.example/story"}]
        self.tools.fetch.side_effect = SourceUnavailable("Publisher declares this content paywalled")
        agent = self.agent([response("search", 0), response("fetch", 0), response("stop")])
        with self.assertRaises(SourceUnavailable):
            await self.recover(agent, "paywall")
        self.tools.browser.assert_not_awaited()

    async def test_cache_failure_stops_before_any_model_or_retrieval_call(self):
        agent = self.agent([])
        with patch.object(self.cache, "get", side_effect=CacheUnavailable("offline")):
            with self.assertRaises(CacheUnavailable):
                await self.recover(agent)
        self.nim.complete.assert_not_awaited()

    async def test_timeout_is_explicit(self):
        async def wait_forever(url):
            await asyncio.Event().wait()
        self.tools.browser.side_effect = wait_forever
        agent = self.agent([response("browser", -1)], timeout=0.02)
        with self.assertRaisesRegex(SourceUnavailable, "time budget"):
            await self.recover(agent)

    async def test_interrupted_verification_resumes_retrieved_content_without_refetch(self):
        agent = self.agent([response("browser", -1), RuntimeError("NIM temporarily down")])
        with self.assertRaisesRegex(RuntimeError, "temporarily down"):
            await self.recover(agent)
        self.nim.complete = AsyncMock(return_value=response(match=True))
        content, _ = await self.recover(agent)
        self.assertEqual(content, self.content)
        self.tools.browser.assert_awaited_once()
        self.nim.complete.assert_awaited_once()

    def test_original_learning_graph_still_runs(self):
        result = build_recovery_graph().invoke({"title": "Example", "original_url": self.original, "failure_kind": "blocked"})
        self.assertEqual(result["action"], "browser")


class RetrievalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_reader_checks_identity_and_rejects_incomplete_sources(self):
        url = "https://openai.com/index/example/"
        for text, allowed in (
            (f"Title: Example\nURL Source: {url}\nMarkdown Content:\n{TEXT}", True),
            (f"Title: Example\nURL Source: https://other.example/\nMarkdown Content:\n{TEXT}", False),
            (f"Title: Example\nURL Source: {url}\nMarkdown Content:\nContent was truncated. {TEXT}", False),
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=text))) as client:
                with patch("recovery_tools.check_destination", new=AsyncMock()):
                    if allowed:
                        content = await RecoveryTools(client).reader(url)
                        self.assertEqual(content.text, TEXT)
                        self.assertEqual(content.url, url)
                    else:
                        with self.assertRaises(SourceUnavailable):
                            await RecoveryTools(client).reader(url)

    async def test_reader_refuses_unknown_and_paywalled_publishers(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail("Unexpected network request"))) as client:
            with self.assertRaises(SourceUnavailable):
                await RecoveryTools(client).reader("https://www.nytimes.com/story")

    async def test_local_and_dns_private_targets_are_rejected(self):
        for url in ("http://127.0.0.1/a", "http://169.254.169.254/", "file:///etc/passwd", "http://localhost/a"):
            with self.assertRaises(SourceUnavailable):
                await check_destination(url)
        fake = [(2, 1, 6, "", ("10.0.0.2", 443))]
        with patch("recovery_tools.socket.getaddrinfo", return_value=fake):
            with self.assertRaises(SourceUnavailable):
                await check_destination("https://private.example")

    async def test_redirect_destinations_are_checked_before_fetching(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tools = RecoveryTools(client)
            async def guard(url):
                if "127.0.0.1" in url:
                    raise SourceUnavailable("private")
            with patch("recovery_tools.check_destination", new=guard):
                with self.assertRaises(SourceUnavailable):
                    await tools.fetch("https://public.example")
        self.assertEqual(len(calls), 1)

    async def test_search_returns_only_candidate_urls_and_titles(self):
        xml = '<rss><channel><item><title>News</title><link>https://news.example/story</link><description>Snippet cannot be article content</description></item></channel></rss>'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))) as client:
            with patch("recovery_tools.check_destination", new=AsyncMock()), patch.object(RecoveryTools, "_metasearch", new=AsyncMock(return_value=[])):
                results = await RecoveryTools(client).search("news")
        self.assertEqual(results, [{"title": "News", "url": "https://news.example/story"}])

    async def test_paywall_markers_are_preserved_in_retrieval(self):
        html = '<html><script type="application/ld+json">{"isAccessibleForFree": false}</script><article>' + TEXT + '</article></html>'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html"}))) as client:
            with patch("recovery_tools.check_destination", new=AsyncMock()):
                with self.assertRaisesRegex(SourceUnavailable, "paywalled"):
                    await RecoveryTools(client).fetch("https://publisher.example")
