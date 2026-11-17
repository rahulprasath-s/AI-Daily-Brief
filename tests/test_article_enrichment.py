import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx

from article_content import ArticleContent, SourceUnavailable, checked_content, extract_html, extract_source
from nim_article_summary import DEFAULT_ARTICLE_MODEL, NimArticleClient, condense_complete_draft, retry_after_seconds, source_attribution, split_full_text, summarize_source, validate_spoken_summary
from story_summary import ready_summary_properties
from summary_cache import SummaryCache, cache_key
from nim_article_summary import PROMPT_VERSION
from summarize_news import NotionWritePending, matches_saved, write_checkpoint
from check_story_summary import ORIGINAL_TYPES


BODY = """The company released a new model for software development on Friday.
Its announcement describes support for code completion, debugging, and reviewing
changes across a repository. The developers say that the model can work with longer
inputs than the earlier release, but the announcement does not provide independent
testing. They recommend checking generated code before using it in a production
service. Access is being introduced gradually, with availability depending on the
customer's region and account. The team also describes limitations involving
unfamiliar libraries and incomplete project context. Further evaluation results
will be published after customers have had time to test the system."""


class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    def test_article_extraction_is_not_title_fallback(self):
        html = f"<html><body><article><h1>A model launch</h1><p>{BODY}</p></article></body></html>"
        result = extract_html(html, "https://example.com/story")
        self.assertIn("does not provide independent", result.text)
        with self.assertRaises(SourceUnavailable):
            extract_html("<html><h1>AI breakthrough</h1></html>", "https://example.com")

    def test_paywall_and_overlength_sources_are_explicitly_unavailable(self):
        with self.assertRaisesRegex(SourceUnavailable, "paywalled"):
            extract_html('<script>{"isAccessibleForFree":false}</script>' + BODY, "https://example.com")
        with self.assertRaisesRegex(SourceUnavailable, "no partial summary"):
            checked_content("source " * 20_000, "article", "https://example.com")

    async def test_huggingface_uses_actual_card_not_page_metadata(self):
        async def handle(request):
            self.assertEqual(request.url.path, "/owner/model/raw/main/README.md")
            return httpx.Response(200, text="---\ntags: [test]\n---\n" + BODY)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = await extract_source(client, "https://huggingface.co/owner/model")
        self.assertEqual(source.kind, "huggingface_model_card")
        self.assertNotIn("tags:", source.text)
        self.assertIn("independent testing", " ".join(source.text.split()))

    async def test_status_page_uses_incident_updates(self):
        async def handle(request):
            self.assertEqual(request.url.path, "/api/v2/incidents/incident123.json")
            return httpx.Response(200, json={
                "name": "Elevated errors", "status": "resolved",
                "incident_updates": [{"status": "resolved", "created_at": "2026-11-14T08:00:00Z", "body": BODY}],
            })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = await extract_source(client, "https://status.example.com/incidents/incident123")
        self.assertEqual(source.kind, "status_incident")
        self.assertIn("resolved", source.text)

    async def test_blocked_sources_are_not_inferred_from_headlines(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
            with self.assertRaisesRegex(SourceUnavailable, "HTTP 403"):
                await extract_source(client, "https://example.com/blocked")

    def test_chunking_preserves_every_character(self):
        text = BODY * 400
        chunks = split_full_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 12_000 for chunk in chunks))


class NimRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_output_is_retried_with_more_budget_not_saved_as_complete(self):
        http = SimpleNamespace(post=AsyncMock(side_effect=[
            httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "Incomplete"}}]}),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Complete result"}}]}),
        ]))
        client = NimArticleClient(http, "fake", min_interval=0, sleep=AsyncMock())
        self.assertEqual(await client.complete([], max_tokens=900), "Complete result")
        self.assertEqual(http.post.await_args_list[1].kwargs["json"]["max_tokens"], 1800)

    async def test_429_and_503_retry_even_with_non_json_error_bodies(self):
        responses = [
            httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
            httpx.Response(503, headers={"Retry-After": "0"}, text="unavailable"),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Success"}}]}),
        ]
        http = SimpleNamespace(post=AsyncMock(side_effect=responses))
        sleep = AsyncMock()
        client = NimArticleClient(http, "fake", min_interval=0, sleep=sleep)
        self.assertEqual(await client.complete([]), "Success")
        self.assertEqual(http.post.await_count, 3)
        self.assertGreaterEqual(sleep.await_args_list[0].args[0], 2)

    async def test_permanent_auth_failure_is_not_retried(self):
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(401)))
        client = NimArticleClient(http, "fake", sleep=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "not retried"):
            await client.complete([])
        with self.assertRaisesRegex(RuntimeError, "fix credentials"):
            await client.complete([])
        http.post.assert_awaited_once()

    async def test_retries_are_finite_and_not_silent_headline_fallbacks(self):
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(429)))
        client = NimArticleClient(http, "fake", min_interval=0, max_attempts=2, sleep=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            await client.complete([])
        self.assertEqual(http.post.await_count, 2)

    def test_retry_after_supports_http_dates(self):
        now = datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Sat, 14 Nov 2026 08:01:00 GMT", now), 60)

    async def test_retry_deadline_preserves_cooldown_for_the_next_story(self):
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(429, headers={"Retry-After": "10"})))
        client = NimArticleClient(http, "fake", min_interval=0, retry_budget=0.02)
        with self.assertRaises(TimeoutError):
            await client.complete([])
        with self.assertRaises(TimeoutError):
            await client.complete([])
        # A new story must not send another request during the server cooldown.
        http.post.assert_awaited_once()

    def test_output_rejects_urls_bullets_and_wrong_lengths(self):
        for text in ("- A bullet list", "Read https://example.com now", "# Heading", "short"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_spoken_summary(text, 15, 200)

    def test_model_card_claims_keep_explicit_source_attribution(self):
        result = source_attribution("Haru is a speech model.", "huggingface_model_card")
        self.assertEqual(result, "According to its model card, Haru is a speech model.")
        self.assertEqual(source_attribution(result, "huggingface_model_card"), result)

    def test_prose_selection_keeps_complete_qualified_claims_and_adds_no_words(self):
        opening = "The author argues that AI tools need careful human review."
        background = "Historical background informs earlier decisions. " * 25
        point = "The speaker says AI can help with small coding tasks but may fail on larger systems. " * 4
        uncertainty = "The speaker questions claims about AI productivity and says the evidence remains uncertain. " * 6
        draft = opening + " " + background + "\n\n" + point + "\n\n" + uncertainty
        result = condense_complete_draft(draft, "AI coding productivity", 150, 200)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(opening))
        self.assertIn("may fail", result)
        self.assertIn("evidence remains uncertain", result)
        self.assertTrue(set(result.split()).issubset(set(draft.split())))
        self.assertLessEqual(len(result.split()), 200)


class CacheAndWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = SummaryCache(self.directory.name)

    def tearDown(self):
        self.cache.close()
        self.directory.cleanup()

    async def test_overlong_single_paragraph_is_rewritten_and_final_is_reused(self):
        source = ArticleContent(BODY * 4, "html_article", "https://example.com")
        draft = " ".join(["detail"] * 450) + " The author says the findings are preliminary."
        revised = "The author says the findings are preliminary. " + " ".join(["detail"] * 160) + "."
        client = SimpleNamespace(model="fake", complete=AsyncMock(side_effect=[draft, revised]))
        result = await summarize_source(client, self.cache, "overlong", "Title", source)
        self.assertEqual(result, revised)
        self.assertLessEqual(len(result.split()), 200)
        payload = json.loads(client.complete.call_args_list[1].args[0][1]["content"])
        self.assertEqual(payload["untrusted_source"], draft)
        self.assertIn("material limitations", payload["task"])
        self.assertEqual(await summarize_source(client, self.cache, "overlong", "Title", source), result)
        self.assertEqual(client.complete.await_count, 2)

    async def test_rewrite_resumes_after_interruption_without_regenerating_source_summary(self):
        source = ArticleContent(BODY * 4, "html_article", "https://example.com")
        first = " ".join(["detail"] * 450)
        still_long = " ".join(["detail"] * 240)
        revised = " ".join(["detail"] * 175)
        client = SimpleNamespace(model="fake", complete=AsyncMock(side_effect=[first, still_long, RuntimeError("temporary failure")]))
        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            await summarize_source(client, self.cache, "resume-rewrite", "Title", source)
        client.complete = AsyncMock(return_value=revised)
        self.assertEqual(await summarize_source(client, self.cache, "resume-rewrite", "Title", source), revised)
        client.complete.assert_awaited_once()
        payload = json.loads(client.complete.call_args.args[0][1]["content"])
        self.assertEqual(payload["untrusted_source"], still_long)

    async def test_short_sources_keep_their_smaller_word_budget(self):
        from nim_article_summary import summary_word_range
        source = ArticleContent(BODY, "html_article", "https://example.com")
        minimum, maximum = summary_word_range(len(BODY.split()))
        draft = " ".join(["detail"] * (maximum + 80))
        revised = " ".join(["detail"] * minimum)
        client = SimpleNamespace(model="fake", complete=AsyncMock(side_effect=[draft, revised]))
        self.assertEqual(await summarize_source(client, self.cache, "short-source", "Title", source), revised)
        payload = json.loads(client.complete.call_args.args[0][1]["content"])
        self.assertIn(f"{minimum} to {maximum} words", payload["task"])

    async def test_repeated_verbose_rewrites_fall_back_to_complete_qualified_sentences(self):
        from nim_article_summary import summary_word_range
        source = ArticleContent(BODY, "html_article", "https://example.com")
        opening = "The company says its AI browser tool supports agents using existing browser sessions."
        caveat = "Users must approve sensitive actions, and access may depend on their account settings."
        extra = "The browser tool includes several optional integrations for development teams working on everyday tasks."
        draft = " ".join([opening, caveat, *([extra] * 5)])
        client = SimpleNamespace(model="fake", complete=AsyncMock(return_value=draft))
        result = await summarize_source(client, self.cache, "sentence-fallback", "AI browser tool", source)
        minimum, maximum = summary_word_range(len(BODY.split()))
        self.assertTrue(minimum <= len(result.split()) <= maximum)
        self.assertTrue(result.startswith(opening))
        self.assertIn(caveat, result)
        self.assertTrue(result.endswith("."))
        self.assertTrue(set(result.split()).issubset(set(draft.split())))
        self.assertEqual(client.complete.await_count, 4)

    async def test_attribution_counts_toward_limit_and_cached_overlong_result_is_repaired(self):
        source = ArticleContent(BODY * 4, "huggingface_model_card", "https://huggingface.co/example/model")
        key = cache_key("cached-overlong", source.fingerprint, "fake", PROMPT_VERSION, "final")
        self.cache.set(key, {"text": " ".join(["detail"] * 200)})
        client = SimpleNamespace(model="fake", complete=AsyncMock(return_value=" ".join(["detail"] * 175)))
        result = await summarize_source(client, self.cache, "cached-overlong", "Title", source)
        self.assertTrue(result.startswith("According to its model card,"))
        self.assertLessEqual(len(result.split()), 200)
        client.complete.assert_awaited_once()

    async def test_failed_rewrites_are_bounded_and_never_saved_as_final(self):
        source = ArticleContent(BODY * 4, "html_article", "https://example.com")
        client = SimpleNamespace(model="fake", complete=AsyncMock(return_value=" ".join(["detail"] * 450)))
        with self.assertRaisesRegex(ValueError, "rewrite failed.*three attempts"):
            await summarize_source(client, self.cache, "always-long", "Title", source)
        self.assertEqual(client.complete.await_count, 4)
        key = cache_key("always-long", source.fingerprint, "fake", PROMPT_VERSION, "final")
        self.assertIsNone(self.cache.get(key))
        with self.assertRaisesRegex(ValueError, "rewrite failed"):
            await summarize_source(client, self.cache, "always-long", "Title", source)
        self.assertEqual(client.complete.await_count, 4)

    async def test_successful_model_result_survives_process_restart(self):
        content = ArticleContent(BODY, "html_article", "https://example.com")
        # BODY is short enough to request a 60–110 or 30–55 word summary.
        from nim_article_summary import summary_word_range
        minimum, maximum = summary_word_range(len(BODY.split()))
        text = " ".join(BODY.split()[:minimum])
        model = SimpleNamespace(model="fake-model", complete=AsyncMock(return_value=text))
        first = await summarize_source(model, self.cache, "page", "Title", content)
        self.cache.close()
        self.cache = SummaryCache(self.directory.name)
        second = await summarize_source(model, self.cache, "page", "Title", content)
        self.assertEqual(first, second)
        model.complete.assert_awaited_once()

    async def test_long_summary_reads_every_segment_before_the_final_merge(self):
        original = BODY * 30
        source = ArticleContent(original, "youtube_transcript", "https://example.com/video")
        segments = []

        async def completion(messages, max_tokens=900):
            payload = json.loads(messages[1]["content"])
            if "segment" in payload:
                segments.append(payload["untrusted_source"])
                return " ".join(["notes"] * 100)
            self.assertEqual(payload["coverage"], "Notes covering all source segments")
            return " ".join(["summary"] * 150)

        client = SimpleNamespace(model="fake", complete=completion)
        result = await summarize_source(client, self.cache, "long-page", "Title", source)
        self.assertEqual("".join(segments), original)
        self.assertEqual(len(result.split()), 150)

    async def test_known_long_context_model_receives_complete_source_within_byte_budget(self):
        original = BODY * 30
        source = ArticleContent(original, "youtube_transcript", "https://example.com/video")
        async def completion(messages, max_tokens=900):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["untrusted_source"], original)
            self.assertEqual(payload["coverage"], "Complete extracted source text")
            return " ".join(["summary"] * 150)
        client = SimpleNamespace(model=DEFAULT_ARTICLE_MODEL, complete=completion)
        result = await summarize_source(client, self.cache, "full-context-page", "Title", source)
        self.assertEqual(len(result.split()), 150)

    async def test_failed_notion_write_keeps_generated_payload_for_later_retry(self):
        properties = {name: {"type": kind} for name, kind in ORIGINAL_TYPES.items()}
        properties.update({"Summary": {"rich_text": []}, "Summary Status": {"select": {"name": "Pending"}}, "Summarized At": {"date": None}})
        page = {"id": "page", "properties": properties}
        payload = ready_summary_properties("A source-backed result.", datetime.now(timezone.utc))
        with (
            patch("summarize_news.update_story_summary", new=AsyncMock(side_effect=RuntimeError("timeout"))),
            patch("summarize_news.request_json", new=AsyncMock(return_value=page)),
            patch("summarize_news.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(NotionWritePending):
                await write_checkpoint(SimpleNamespace(), page, payload, self.cache, "identity")
        self.assertEqual(self.cache.get("pending_write:page")["properties"], payload)

    def test_readback_matches_notion_normalized_rich_text(self):
        stamp = datetime.now(timezone.utc)
        payload = ready_summary_properties("Saved result.", stamp)
        response = {
            "Summary": {"rich_text": [{"plain_text": "Saved result."}]},
            "Summary Status": {"select": {"name": "Ready"}},
            "Summarized At": {"date": {"start": stamp.isoformat()}},
        }
        self.assertTrue(matches_saved(response, payload))

    def test_readback_accepts_notion_minute_precision_for_old_checkpoints(self):
        payload = ready_summary_properties("Saved result.", datetime(2026, 11, 14, 15, 15, tzinfo=timezone.utc))
        payload["Summarized At"]["date"]["start"] = "2026-11-14T15:15:22.879453+00:00"
        response = {
            "Summary": {"rich_text": [{"plain_text": "Saved result."}]},
            "Summary Status": {"select": {"name": "Ready"}},
            "Summarized At": {"date": {"start": "2026-11-14T15:15:00.000+00:00"}},
        }
        self.assertTrue(matches_saved(response, payload))
