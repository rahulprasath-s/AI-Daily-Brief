from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from enrichment_date import resolve_date


class DeliveryDateTests(unittest.TestCase):
    def test_delayed_workflow_uses_original_delivery_date(self):
        self.assertEqual(resolve_date({"ok": True, "mode": "publish", "date": "2026-10-25"}), "2026-10-25")

    def test_missing_or_unsuccessful_context_is_refused(self):
        for context in ({}, {"ok": False}, {"ok": True, "mode": "dry-run"}, {"ok": True, "mode": "publish"}):
            with self.assertRaises(ValueError):
                resolve_date(context)

    def test_manual_date_must_be_safe_and_canonical(self):
        self.assertEqual(resolve_date(manual_date="2026-11-14"), "2026-11-14")
        for value in ("20261114", "2026-11-14\ninjected=value", "2026-02-30"):
            with self.assertRaises(ValueError):
                resolve_date(manual_date=value)
