"""Resolve a trusted delivery date without guessing when a delayed run crossed midnight."""

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def resolve_date(context=None, manual_date=None):
    if context is not None:
        if context.get("ok") is not True or context.get("mode") != "publish":
            raise ValueError("Enrichment requires a successful publish result.")
        value = context.get("date")
        if not isinstance(value, str):
            raise ValueError("Delivery result is missing its digest date.")
    else:
        value = manual_date or datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Expected a YYYY-MM-DD digest date.")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--date")
    args = parser.parse_args()
    result = resolve_date(json.loads(args.context.read_text()) if args.context else None, args.date)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"date={result}\n")
    print(f"Enriching delivered digest: {result}")
