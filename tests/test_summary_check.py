from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_story_summary import DATABASE_ID, ORIGINAL_TYPES, verify_target, verify_update


def untouched_page():
    properties = {name: {"type": kind} for name, kind in ORIGINAL_TYPES.items()}
    properties.update({
        "Type": {"select": {"name": "news"}},
        "Summary": {"rich_text": []},
        "Summary Status": {"select": None},
        "Summarized At": {"date": None},
    })
    return {"parent": {"database_id": DATABASE_ID}, "properties": properties}


class SummaryCheckTests(unittest.TestCase):
    def test_untouched_news_page_is_eligible(self):
        for option in ("news", "NEWS"):
            with self.subTest(option=option):
                page = untouched_page()
                page["properties"]["Type"]["select"]["name"] = option
                verify_target(page)

    def test_paper_is_rejected(self):
        page = untouched_page()
        page["properties"]["Type"]["select"]["name"] = "paper"
        with self.assertRaisesRegex(ValueError, "not a news record"):
            verify_target(page)

    def test_ready_or_populated_pages_are_not_overwritten(self):
        for change in (
            {"Summary Status": {"select": {"name": "Ready"}}},
            {"Summary": {"rich_text": [{"plain_text": "Existing summary"}]}},
            {"Summarized At": {"date": {"start": "2026-11-14"}}},
        ):
            with self.subTest(change=change):
                page = untouched_page()
                page["properties"].update(change)
                with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                    verify_target(page)

    def test_wrong_database_is_rejected(self):
        page = untouched_page()
        page["parent"]["database_id"] = "11111111-2222-4333-8444-555555555555"
        with self.assertRaisesRegex(ValueError, "another database"):
            verify_target(page)

    def test_update_verification_detects_original_field_changes(self):
        before = untouched_page()
        after = deepcopy(before)
        after["properties"]["Summary Status"] = {"select": {"name": "Pending"}}
        verify_update(before, after)
        after["properties"]["Description"] = {"rich_text": []}
        with self.assertRaisesRegex(ValueError, "Description"):
            verify_update(before, after)
