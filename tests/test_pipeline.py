import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

workers_module = types.ModuleType("workers")
workers_module.fetch = None
sys.modules.setdefault("workers", workers_module)

js_module = types.ModuleType("js")
js_module.fetch = None
sys.modules.setdefault("js", js_module)

import digest  # noqa: E402
import cli  # noqa: E402
import http_client  # noqa: E402
from digest import (  # noqa: E402
    _exclude_previously_published,
    _filter_items_to_window,
    _news_window,
)
from nim import add_nim_summaries, finalize_news_for_publish, summary_similarity  # noqa: E402
import notion  # noqa: E402
from notion import _notion_properties, item_identities  # noqa: E402
from sources.techmeme import _parse_techmeme_feed  # noqa: E402


class Env:
    DIGEST_TIMEZONE = "Europe/Berlin"
    NOTION_TOKEN = "test-token"
    NOTION_DATABASE_ID = "test-database"


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_publish_date_uses_digest_timezone(self):
        self.assertEqual(
            cli.parse_publish_date("2026-08-04", Env()),
            datetime(2026, 8, 4, 10, tzinfo=timezone.utc),
        )

    def test_cli_rejects_invalid_publish_date(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            cli.parse_publish_date("August 4", Env())

    async def test_portable_response_parses_json(self):
        response = http_client._PortableResponse(200, '{"ok": true}')

        self.assertTrue(response.ok)
        self.assertEqual(await http_client.response_json(response), {"ok": True})

    async def test_cli_dry_run_never_publishes(self):
        digest_result = {
            "date": "2026-08-06",
            "content_date": "2026-08-05",
            "news": [],
            "stats": {},
        }

        with (
            patch.object(cli, "get_published_items", new=AsyncMock(return_value=[])),
            patch.object(cli, "build_digest", new=AsyncMock(return_value=digest_result)),
            patch.object(
                cli,
                "filter_missing_digest_items",
                new=AsyncMock(return_value=digest_result),
            ),
            patch.object(
                cli,
                "add_nim_summaries",
                new=AsyncMock(return_value=digest_result),
            ),
            patch.object(
                cli,
                "finalize_news_for_publish",
                return_value=digest_result,
            ),
            patch.object(
                cli,
                "publish_digest_to_notion",
                new=AsyncMock(),
            ) as publish_mock,
        ):
            result = await cli.run_digest(Env(), publish=False)

        self.assertEqual(result["mode"], "dry-run")
        self.assertIsNone(result["notion"])
        self.assertNotIn("papers", result)
        self.assertNotIn("missing_papers", result)
        publish_mock.assert_not_awaited()

    def test_window_is_exact_previous_berlin_calendar_day(self):
        window = _news_window(
            Env(),
            now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
        )

        self.assertEqual(window["window_start_local"].isoformat(), "2026-07-13T00:00:00+02:00")
        self.assertEqual(window["window_end_local"].isoformat(), "2026-07-14T00:00:00+02:00")

    async def test_digest_collects_only_news_and_caps_output_at_ten(self):
        titles = [
            "OpenAI reasoning release", "Nvidia inference accelerator",
            "Robotics tactile manipulation", "Multilingual speech recognition",
            "Protein folding discovery", "Autonomous driving safety",
            "Image generation licensing", "Datacenter energy consumption",
            "European artificial intelligence regulation", "Enterprise agent security",
            "Neural weather forecasting", "Semiconductor export restrictions",
            "Quantum error correction", "Satellite wildfire detection",
            "Agricultural crop monitoring", "Underwater acoustic navigation",
            "Battery materials synthesis", "Education tutoring platform",
            "Financial fraud prevention", "Accessible sign language translation",
            "Hospital diagnostic imaging", "Factory predictive maintenance",
        ]
        stories = [{
            "type": "news", "source": "hacker news", "title": title,
            "url": f"https://example.com/{index}",
            "published_at": "2026-07-13T12:00:00Z", "score_hint": index,
        } for index, title in enumerate(titles)]
        with (
            patch.object(digest, "fetch_hackernews_ai_stories", new=AsyncMock(return_value=stories)) as hn,
            patch.object(digest, "fetch_huggingface_ai_models", new=AsyncMock(return_value=[])) as hf,
            patch.object(digest, "fetch_techmeme_ai_stories", new=AsyncMock(return_value=[])) as tm,
        ):
            result = await digest.build_digest(
                now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
                published_items=[],
            )
        self.assertNotIn("papers", result)
        self.assertEqual(len(result["news"]), 10)
        self.assertEqual([item["rank"] for item in result["news"]], [f"N{i}" for i in range(1, 11)])
        self.assertTrue(all(item["type"] == "news" for item in result["news"]))
        for source in (hn, hf, tm):
            source.assert_awaited_once()
        self.assertEqual(str(hn.call_args.kwargs["window_start_utc"]), "2026-07-12 22:00:00+00:00")

        with (
            patch.object(digest, "fetch_hackernews_ai_stories", new=AsyncMock(return_value=stories)),
            patch.object(digest, "fetch_huggingface_ai_models", new=AsyncMock(return_value=[])),
            patch.object(digest, "fetch_techmeme_ai_stories", new=AsyncMock(return_value=[])),
            patch.object(digest, "rank_news_with_nim", new=AsyncMock(return_value=stories[:10])) as rank,
        ):
            result = await digest.build_digest(
                Env(), now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
                published_items=[],
            )
        self.assertEqual(len(result["news"]), 10)
        self.assertEqual(len(rank.call_args.args[1]), len(titles))
        self.assertIn(stories[0], rank.call_args.args[1])
        self.assertEqual(rank.call_args.kwargs["limit"], 10)

    async def test_news_candidates_are_ranked_with_nim_after_history_filtering(self):
        seen = {"type": "news", "title": "Existing release", "url": "https://example.com/seen"}
        fresh = {"type": "news", "title": "New robotics breakthrough", "url": "https://example.com/fresh"}
        for item in (seen, fresh):
            item["published_at"] = "2026-07-13T12:00:00Z"
        with (
            patch.object(digest, "fetch_hackernews_ai_stories", new=AsyncMock(return_value=[seen, fresh])),
            patch.object(digest, "fetch_huggingface_ai_models", new=AsyncMock(return_value=[])),
            patch.object(digest, "fetch_techmeme_ai_stories", new=AsyncMock(return_value=[])),
            patch.object(digest, "rank_news_with_nim", new=AsyncMock(return_value=[fresh])) as rank,
        ):
            result = await digest.build_digest(
                Env(), now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
                published_items=[seen],
            )
        self.assertEqual(result["news"], [fresh])
        rank.assert_awaited_once_with(unittest.mock.ANY, [fresh], limit=10)
        self.assertEqual(result["stats"]["news_skipped_history_identity"], 1)

    async def test_legacy_papers_are_never_summarized_or_published(self):
        legacy_paper = {"type": "paper", "rank": "P1", "title": "Legacy paper"}
        payload = {"date": "2026-07-14", "papers": [legacy_paper], "news": []}
        summarized = await add_nim_summaries(Env(), payload)
        self.assertNotIn("summary", legacy_paper)
        with patch.object(notion, "_create_page", new=AsyncMock()) as create:
            result = await notion.publish_digest_to_notion(Env(), summarized, published_items=[])
        self.assertEqual(result["created"], 0)
        create.assert_not_awaited()

    def test_history_match_uses_title_even_when_url_changes(self):
        candidate = {
            "title": "OpenAI launches a new reasoning model",
            "url": "https://example.com/new-link",
        }
        published_identities = {
            ("title", "openai launches a new reasoning model"),
            ("url", "https://example.com/old-link"),
        }

        result = _exclude_previously_published([candidate], published_identities)

        self.assertEqual(result["items"], [])
        self.assertEqual(result["skipped"], [candidate])

    def test_item_identities_include_link_and_title(self):
        identities = item_identities({
            "title": "  An AI Story  ",
            "url": "HTTPS://Example.com/story/?b=2&a=1#section",
        })

        self.assertEqual(identities, {
            ("title", "an ai story"),
            ("url", "https://example.com/story?a=1&b=2"),
        })

    def test_empty_notion_type_select_is_treated_as_unknown(self):
        page = {
            "properties": {
                "Type": {"select": None},
                "Title": {"title": []},
                "Description": {"rich_text": []},
                "Link": {"url": None},
            },
        }

        self.assertEqual(notion._page_as_item(page), {
            "id": None,
            "date": "",
            "rank": "",
            "title": "",
            "url": "",
            "type": "",
            "summary": "",
            "summary_input": "",
        })

    def test_new_notion_rows_default_to_not_yet(self):
        payload = _notion_properties(
            "test-database",
            "2026-08-04",
            {
                "type": "news",
                "title": "AI update",
                "source": "hacker news",
                "url": "https://example.com/ai-update",
                "rank": "N1",
            },
        )

        self.assertEqual(
            payload["properties"]["Status"],
            {"status": {"name": "NOT YET"}},
        )

    def test_summary_similarity_detects_shared_story_content(self):
        original = (
            "OpenAI released a reasoning model for developers with improved "
            "coding accuracy and lower inference costs."
        )
        syndicated = (
            "The company said OpenAI released a reasoning model for developers "
            "with improved coding accuracy and lower inference costs worldwide."
        )

        self.assertGreaterEqual(summary_similarity(original, syndicated), 0.7)

    def test_reconciliation_assigns_next_free_news_ranks(self):
        digest = {
            "existing_news_ranks": ["N1", "N2", "N3"],
            "news": [{
                "type": "news",
                "title": "New accelerator launch",
                "summary": "A chip vendor launched an accelerator for inference clusters.",
            }, {
                "type": "news",
                "title": "Robotics research result",
                "summary": "Researchers demonstrated dexterous manipulation using tactile sensors.",
            }, {
                "type": "news",
                "title": "Speech model release",
                "summary": "An open multilingual speech model improves low-resource transcription.",
            }],
        }

        result = finalize_news_for_publish(digest)

        self.assertEqual(
            [item["rank"] for item in result["news"]],
            ["N4", "N5", "N6"],
        )

    def test_techmeme_uses_each_rss_item_publication_time(self):
        feed = """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>OpenAI launches a new AI model for developers</title>
          <link>https://www.techmeme.com/260713/p1</link>
          <pubDate>Mon, 13 Jul 2026 18:30:00 -0400</pubDate>
        </item></channel></rss>"""

        items = _parse_techmeme_feed(feed)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published_at"], "2026-07-13T22:30:00+00:00")

    async def test_publish_guard_compares_summary_with_all_history(self):
        historical_summary = (
            "OpenAI released a reasoning model for developers with improved "
            "coding accuracy and lower inference costs."
        )
        digest = {
            "date": "2026-07-14",
            "news": [{
                "rank": "N1",
                "type": "news",
                "title": "A differently worded syndicated headline",
                "url": "https://new-publisher.example/story",
                "summary": (
                    "The company said OpenAI released a reasoning model for "
                    "developers with improved coding accuracy and lower "
                    "inference costs worldwide."
                ),
            }],
        }

        with (
            patch.object(
                notion,
                "_query_existing_pages_for_date",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                notion,
                "get_published_items",
                new=AsyncMock(return_value=[{
                    "type": "news",
                    "title": "Original headline",
                    "url": "https://original.example/story",
                    "summary": historical_summary,
                }]),
            ),
            patch.object(notion, "_create_page", new=AsyncMock()) as create_page,
        ):
            result = await notion.publish_digest_to_notion(Env(), digest)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_pages"][0]["reason"], "duplicate_history_summary")
        create_page.assert_not_awaited()

    async def test_prefetched_history_avoids_redundant_notion_queries(self):
        published_items = [{
            "id": "existing-page",
            "date": "2026-08-03",
            "rank": "N1",
            "type": "news",
            "title": "Existing news story",
            "url": "https://example.com/story",
            "summary": "Existing summary",
        }]
        digest = {
            "date": "2026-08-03",
            "news": [{
                "rank": "N1",
                "type": "news",
                "title": "Existing news story",
                "url": "https://example.com/story",
            }],
        }

        with (
            patch.object(
                notion,
                "_query_existing_pages_for_date",
                new=AsyncMock(side_effect=AssertionError("unexpected date query")),
            ),
            patch.object(
                notion,
                "get_published_items",
                new=AsyncMock(side_effect=AssertionError("unexpected history query")),
            ),
            patch.object(notion, "_create_page", new=AsyncMock()) as create_page,
        ):
            filtered = await notion.filter_missing_digest_items(
                Env(),
                digest,
                published_items=published_items,
            )
            result = await notion.publish_digest_to_notion(
                Env(),
                digest,
                published_items=published_items,
            )

        self.assertEqual(filtered["news"], [])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_pages"][0]["reason"], "duplicate_date_rank")
        create_page.assert_not_awaited()

    async def test_one_failed_source_does_not_suppress_other_news(self):
        story = {
            "type": "news",
            "source": "techmeme",
            "title": "OpenAI launches a new model",
            "url": "https://example.com/openai-model",
            "published_at": "2026-07-13T12:00:00Z",
            "summary_input": "OpenAI launches a new model",
        }

        with (
            patch.object(
                digest,
                "fetch_hackernews_ai_stories",
                new=AsyncMock(side_effect=RuntimeError("Hacker News unavailable")),
            ),
            patch.object(
                digest,
                "fetch_huggingface_ai_models",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_techmeme_ai_stories",
                new=AsyncMock(return_value=[story]),
            ),
        ):
            result = await digest.build_digest(
                now_utc=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(result["news"], [story])
        self.assertEqual(result["stats"]["source_errors"], {
            "hackernews": "Hacker News unavailable",
        })


if __name__ == "__main__":
    unittest.main()
