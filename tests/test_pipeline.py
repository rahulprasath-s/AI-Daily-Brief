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
from digest import (  # noqa: E402
    _exclude_previously_published,
    _filter_items_to_window,
    _news_window,
    _paper_lookback_window,
)
from nim import summary_similarity  # noqa: E402
import notion  # noqa: E402
from notion import item_identities  # noqa: E402
from sources import arxiv  # noqa: E402
from sources.techmeme import _parse_techmeme_feed  # noqa: E402


class Env:
    DIGEST_TIMEZONE = "Europe/Berlin"
    NOTION_TOKEN = "test-token"
    NOTION_DATABASE_ID = "test-database"


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_arxiv_retries_transient_failure(self):
        class FakeResponse:
            def __init__(self, ok, status, body=""):
                self.ok = ok
                self.status = status
                self._body = body

            async def text(self):
                return self._body

        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>http://arxiv.org/abs/2607.18237v1</id>
          <title>A new AI paper</title>
          <summary>Paper abstract.</summary>
          <published>2026-07-20T17:59:51Z</published>
          <updated>2026-07-20T17:59:51Z</updated>
          <author><name>Researcher</name></author>
        </entry></feed>"""

        with (
            patch.object(
                arxiv,
                "fetch",
                new=AsyncMock(side_effect=[
                    FakeResponse(False, 503),
                    FakeResponse(True, 200, feed),
                ]),
            ) as fetch_mock,
            patch.object(
                arxiv.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep_mock,
        ):
            papers = await arxiv.fetch_arxiv_papers(limit=1)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["title"], "A new AI paper")
        self.assertEqual(fetch_mock.await_count, 2)
        sleep_mock.assert_awaited_once_with(1)

    def test_window_is_exact_previous_berlin_calendar_day(self):
        window = _news_window(
            Env(),
            now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
        )

        self.assertEqual(window["window_start_local"].isoformat(), "2026-07-13T00:00:00+02:00")
        self.assertEqual(window["window_end_local"].isoformat(), "2026-07-14T00:00:00+02:00")

    def test_old_arxiv_paper_is_not_revived_by_update_timestamp(self):
        window = _news_window(
            Env(),
            now_utc=datetime(2026, 7, 14, 6, tzinfo=timezone.utc),
        )
        paper = {
            "source": "arXiv",
            "published_at": "2026-07-10T12:00:00Z",
            "updated_at": "2026-07-13T12:00:00Z",
        }

        result = _filter_items_to_window(
            [paper],
            window["window_start_utc"],
            window["window_end_utc"],
        )

        self.assertEqual(result["items"], [])
        self.assertEqual(result["skipped_outside_window"], 1)

    def test_paper_lookback_covers_delayed_weekend_announcement(self):
        news_window = _news_window(
            Env(),
            now_utc=datetime(2026, 7, 20, 6, tzinfo=timezone.utc),
        )
        paper_window = _paper_lookback_window(news_window)
        paper = {
            "source": "arXiv",
            "published_at": "2026-07-17T17:58:29Z",
        }

        result = _filter_items_to_window(
            [paper],
            paper_window["window_start_utc"],
            paper_window["window_end_utc"],
        )

        self.assertEqual(result["items"], [paper])
        self.assertEqual(paper_window["days"], 7)

    async def test_build_digest_selects_unseen_delayed_arxiv_paper(self):
        paper = {
            "type": "paper",
            "source": "arXiv",
            "title": "PagedWeight",
            "url": "http://arxiv.org/abs/2607.16184v1",
            "published_at": "2026-07-17T17:58:29Z",
            "updated_at": "2026-07-17T17:58:29Z",
            "summary_input": "Efficient MoE serving.",
        }

        with (
            patch.object(
                digest,
                "get_published_items",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_arxiv_papers",
                new=AsyncMock(return_value=[paper]),
            ),
            patch.object(
                digest,
                "fetch_hackernews_ai_stories",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_huggingface_ai_models",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_techmeme_ai_stories",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await digest.build_digest(
                Env(),
                now_utc=datetime(2026, 7, 20, 6, tzinfo=timezone.utc),
            )

        self.assertEqual(result["papers"], [paper])
        self.assertEqual(result["stats"]["papers_in_lookback"], 1)
        self.assertEqual(result["stats"]["papers_considered"], 1)

    async def test_build_digest_skips_delayed_paper_already_in_notion(self):
        paper = {
            "type": "paper",
            "source": "arXiv",
            "title": "PagedWeight",
            "url": "http://arxiv.org/abs/2607.16184v1",
            "published_at": "2026-07-17T17:58:29Z",
            "updated_at": "2026-07-17T17:58:29Z",
            "summary_input": "Efficient MoE serving.",
        }

        with (
            patch.object(
                digest,
                "get_published_items",
                new=AsyncMock(return_value=[{
                    "type": "paper",
                    "title": paper["title"],
                    "url": paper["url"],
                }]),
            ),
            patch.object(
                digest,
                "fetch_arxiv_papers",
                new=AsyncMock(return_value=[paper]),
            ),
            patch.object(
                digest,
                "fetch_hackernews_ai_stories",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_huggingface_ai_models",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                digest,
                "fetch_techmeme_ai_stories",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await digest.build_digest(
                Env(),
                now_utc=datetime(2026, 7, 20, 6, tzinfo=timezone.utc),
            )

        self.assertEqual(result["papers"], [])
        self.assertEqual(result["stats"]["papers_skipped_history_identity"], 1)

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
            "title": "",
            "url": "",
            "type": "",
            "summary": "",
            "summary_input": "",
        })

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
            "papers": [],
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
                "fetch_arxiv_papers",
                new=AsyncMock(side_effect=RuntimeError("arXiv unavailable")),
            ),
            patch.object(
                digest,
                "fetch_hackernews_ai_stories",
                new=AsyncMock(return_value=[]),
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
            "arxiv": "arXiv unavailable",
        })


if __name__ == "__main__":
    unittest.main()
