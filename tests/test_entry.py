import importlib
import json
import re
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = json.loads(body)
        self.status = status


workers = types.ModuleType("workers")
workers.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
workers.Response = FakeResponse
workers.fetch = None
with patch.dict(sys.modules, {"workers": workers}):
    entry = importlib.import_module("entry")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = entry.Default()
        self.worker.env = types.SimpleNamespace(DIGEST_TIMEZONE="Europe/Berlin")

    async def test_status_matches_single_berlin_workflow_schedule(self):
        response = await self.worker.fetch(types.SimpleNamespace(
            method="GET", url="https://example.com/status",
        ))
        schedule = response.body["schedule"]
        workflow = (ROOT / ".github/workflows/daily-brief.yml").read_text()
        self.assertEqual(re.findall(r'- cron: "([^"]+)"', workflow), [schedule["cron"]])
        self.assertEqual(re.findall(r'timezone: "([^"]+)"', workflow), [schedule["timezone"]])
        self.assertEqual(schedule["owner"], "github_actions")
        self.assertNotIn("arXiv", response.body["sources"])
        self.assertNotIn("reconciliation_cron_utc", response.body)

        minute, hour, *_ = schedule["cron"].split()
        berlin = ZoneInfo(schedule["timezone"])
        # The local trigger stays at 08:00 before and after both DST changes.
        for month, day, utc_hour in ((1, 15, 7), (3, 28, 7), (3, 29, 6),
                                     (7, 15, 6), (10, 24, 6), (10, 25, 7)):
            with self.subTest(month=month, day=day):
                trigger = datetime(2026, month, day, int(hour), int(minute), tzinfo=berlin)
                self.assertEqual((trigger.hour, trigger.minute), (8, 0))
                self.assertEqual(trigger.astimezone(timezone.utc).hour, utc_hour)

    async def test_arxiv_debug_route_is_removed(self):
        response = await self.worker.fetch(types.SimpleNamespace(
            method="GET", url="https://example.com/debug/arxiv",
        ))
        self.assertEqual(response.status, 404)

    async def test_nim_debug_summarizes_one_news_item(self):
        news = [{"type": "news", "title": "First"}, {"type": "news", "title": "Second"}]
        with (
            patch.object(entry, "build_digest", new=AsyncMock(return_value={"news": news})),
            patch.object(entry, "add_nim_summaries", new=AsyncMock(
                side_effect=lambda env, digest: digest,
            )) as summarize,
        ):
            response = await self.worker.fetch(types.SimpleNamespace(
                method="POST", url="https://example.com/debug/nim",
            ))
        self.assertEqual(response.body["item"], news[0])
        self.assertEqual(summarize.call_args.args[1]["news"], news[:1])


if __name__ == "__main__":
    unittest.main()
