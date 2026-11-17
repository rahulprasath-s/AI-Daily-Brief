from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from article_content import ArticleContent, SourceUnavailable
from summary_cache import SummaryCache
from summarize_news import run


class RecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_updates_only_unresolved_existing_page(self):
        def page(identifier, status):
            return {"id": identifier, "properties": {
                "Title": {"title": [{"plain_text": "Example story"}]},
                "Link": {"url": "https://publisher.example/story"},
                "Rank": {"rich_text": [{"plain_text": "N1"}]},
                "Summary Status": {"select": {"name": status}},
            }}

        pages = [page("already-ready", "Ready"), page("unavailable", "Unavailable")]
        content = ArticleContent("Full retrieved article content. " * 80, "recovered_alternative", "https://primary.example/story")
        recover = AsyncMock(return_value=(content, [{"tool": "verify", "result": "matched"}]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = SummaryCache(root / ".summary-cache")
            with (
                patch("summarize_news.ROOT", root),
                patch("summarize_news.load_config", return_value=SimpleNamespace(NVIDIA_NIM_API_KEY="test", NVIDIA_NIM_ARTICLE_MODEL=None)),
                patch("summarize_news.request_json", new=AsyncMock(return_value={})),
                patch("summarize_news.verify_schema"),
                patch("summarize_news.fetch_pages", new=AsyncMock(return_value=pages)),
                patch("summarize_news.open_summary_cache", return_value=cache),
                patch("summarize_news.NimArticleClient", return_value=SimpleNamespace(model="fake", requests=0)),
                patch("summarize_news.extract_source", new=AsyncMock(side_effect=SourceUnavailable("HTTP 403"))),
                patch("article_recovery.RecoveryAgent", return_value=SimpleNamespace(recover=recover)),
                patch("summarize_news.summarize_source", new=AsyncMock(return_value="Validated alternative-source summary.")),
                patch("summarize_news.write_checkpoint", new=AsyncMock(return_value=pages[1])) as write,
            ):
                result = await run("2026-11-17", publish=True, recover_unavailable=True, retry_unresolved=True)
            self.assertEqual(result["counts"], {"Ready": 1})
            self.assertEqual(result["stories"][0]["recovered_source_url"], content.url)
            recover.assert_awaited_once()
            self.assertEqual(write.await_args.args[1]["id"], "unavailable")
            properties = write.await_args.args[2]
            self.assertEqual(set(properties), {"Summary", "Summary Status", "Summarized At"})

    async def test_extraction_only_mode_never_implicitly_calls_recovery_model(self):
        with self.assertRaisesRegex(ValueError, "explicitly pass --publish"):
            await run("2026-11-17", recover_unavailable=True)
