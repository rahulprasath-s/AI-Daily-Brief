import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import notion
from story_summary import ready_summary_properties


PAGE_ID = "11111111-2222-4333-8444-555555555555"


class StorySummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = SimpleNamespace(NOTION_TOKEN="fake-test-token")
        self.properties = ready_summary_properties(
            "This is storage test text, not a real article summary.",
            datetime(2026, 11, 14, 8, tzinfo=timezone.utc),
        )

    async def test_updates_existing_page_with_only_summary_fields(self):
        response = SimpleNamespace(
            ok=True,
            status=200,
            json=AsyncMock(return_value={
                "object": "page",
                "id": PAGE_ID,
            }),
        )

        with patch.object(
            notion, "fetch", new=AsyncMock(return_value=response)
        ) as fetch:
            result = await notion.update_story_summary(
                self.env, PAGE_ID, self.properties
            )

        fetch.assert_awaited_once()
        args, kwargs = fetch.call_args

        self.assertEqual(
            args[0],
            f"https://api.notion.com/v1/pages/{PAGE_ID}",
        )
        self.assertEqual(kwargs["method"], "PATCH")
        self.assertEqual(
            kwargs["headers"]["notion-version"],
            notion.NOTION_VERSION,
        )

        payload = json.loads(kwargs["body"])
        self.assertEqual(payload, {"properties": self.properties})
        self.assertNotIn("parent", payload)
        self.assertNotIn("Description", payload["properties"])
        self.assertNotIn("Status", payload["properties"])
        self.assertEqual(result["id"], PAGE_ID)

    async def test_rejects_changes_to_existing_fields(self):
        unsafe = {
            **self.properties,
            "Status": {"status": {"name": "Done"}},
        }

        with patch.object(notion, "fetch", new=AsyncMock()) as fetch:
            with self.assertRaisesRegex(ValueError, "three summary"):
                await notion.update_story_summary(
                    self.env, PAGE_ID, unsafe
                )

            fetch.assert_not_awaited()

    async def test_reports_api_failure_without_creating_a_page(self):
        response = SimpleNamespace(ok=False, status=429)

        with patch.object(
            notion, "fetch", new=AsyncMock(return_value=response)
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                await notion.update_story_summary(
                    self.env, PAGE_ID, self.properties
                )

        fetch.assert_awaited_once()
        self.assertEqual(fetch.call_args.kwargs["method"], "PATCH")

    async def test_request_has_a_bounded_timeout(self):
        async def stalled_request(*args, **kwargs):
            await asyncio.Event().wait()

        with patch.object(notion, "fetch", side_effect=stalled_request):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await notion.update_story_summary(
                    self.env,
                    PAGE_ID,
                    self.properties,
                    timeout_seconds=0.01,
                )


if __name__ == "__main__":
    unittest.main()