"""Verify the summary schema; optionally mark one untouched news page Pending.

Default: read-only. Pass --mark-pending for one guarded update. No Claude calls.
"""

import argparse
import asyncio
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from http_client import fetch, response_json
from notion import NOTION_DATABASES_URL, NOTION_PAGES_URL, NOTION_VERSION, update_story_summary
from story_summary import pending_summary_properties


DATABASE_ID = "380528fa-011e-80c9-95dd-ed251437b140"
DATA_SOURCE_ID = "380528fa-011e-8071-b062-000bc5926829"
ORIGINAL_TYPES = {
    "Date": "date", "Title": "title", "Description": "rich_text",
    "Link": "url", "Rank": "rich_text", "Source": "select",
    "Type": "select", "Status": "status",
}


def load_config():
    config = dict(os.environ)
    path = ROOT / ".dev.vars"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.removeprefix("export ").split("=", 1)
            values = shlex.split(raw, comments=True)
            config.setdefault(key.strip(), values[0] if values else "")
    if not config.get("NOTION_TOKEN"):
        raise ValueError("Missing NOTION_TOKEN")
    if UUID(config.get("NOTION_DATABASE_ID", "")) != UUID(DATABASE_ID):
        raise ValueError("Configured database ID does not match the intended database.")
    return SimpleNamespace(
        NOTION_TOKEN=config["NOTION_TOKEN"],
        NVIDIA_NIM_API_KEY=config.get("NVIDIA_NIM_API_KEY"),
        NVIDIA_NIM_ARTICLE_MODEL=config.get("NVIDIA_NIM_ARTICLE_MODEL"),
        SUMMARY_CACHE_URL=config.get("SUMMARY_CACHE_URL"),
        SUMMARY_CACHE_TOKEN=config.get("SUMMARY_CACHE_TOKEN"),
    )


def verify_schema(database):
    properties = database.get("properties", {})
    expected = {
        **ORIGINAL_TYPES,
        "Summary": "rich_text", "Summary Status": "select", "Summarized At": "date",
    }
    for name, kind in expected.items():
        if properties.get(name, {}).get("type") != kind:
            raise ValueError(f"Missing property or incorrect type: {name} (expected {kind})")
    options = {item["name"] for item in properties["Summary Status"]["select"]["options"]}
    if options != {"Pending", "Ready", "Unavailable", "Failed"}:
        raise ValueError("Summary Status must have Pending, Ready, Unavailable, and Failed options.")


def verify_target(page):
    parent = page.get("parent", {})
    allowed = {str(UUID(DATABASE_ID)), str(UUID(DATA_SOURCE_ID))}
    parent_ids = {
        str(UUID(parent[key]))
        for key in ("database_id", "data_source_id") if parent.get(key)
    }
    if not parent_ids.intersection(allowed):
        raise ValueError("Page belongs to another database/data source.")
    if page.get("archived") or page.get("in_trash"):
        raise ValueError("Page is archived or trashed.")
    properties = page["properties"]
    if (properties["Type"].get("select") or {}).get("name", "").casefold() != "news":
        raise ValueError("Page is not a news record.")
    status = (properties["Summary Status"].get("select") or {}).get("name")
    if (
        properties["Summary"].get("rich_text")
        or status not in (None, "Pending")
        or properties["Summarized At"].get("date")
    ):
        raise ValueError("Summary fields already contain data; refusing to overwrite them.")


def verify_update(before, after):
    old, new = before["properties"], after["properties"]
    for name in ORIGINAL_TYPES:
        if old[name] != new[name]:
            raise ValueError(f"Original property changed: {name}")
    if (
        (new["Summary Status"].get("select") or {}).get("name") != "Pending"
        or new["Summary"].get("rich_text")
        or new["Summarized At"].get("date")
    ):
        raise ValueError("Pending update could not be verified. Inspect the page before retrying.")


async def request_json(env, url, method="GET", body=None):
    import json

    async with asyncio.timeout(20):
        response = await fetch(
            url, method=method,
            headers={
                "authorization": f"Bearer {env.NOTION_TOKEN}",
                "notion-version": NOTION_VERSION,
                "content-type": "application/json",
            },
            body=json.dumps(body) if body is not None else None,
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Notion request failed: HTTP {response.status}")
        return await response_json(response)


async def main(mark_pending=False, page_id=None):
    env = load_config()
    database = await request_json(env, f"{NOTION_DATABASES_URL}/{DATABASE_ID}")
    verify_schema(database)
    print("Summary schema verified; all eight original property types match.")
    if not mark_pending:
        print("Read-only check complete. No records were changed.")
        return

    if page_id is None:
        news_options = [
            option["name"]
            for option in database["properties"]["Type"]["select"]["options"]
            if option["name"].casefold() == "news"
        ]
        if len(news_options) != 1:
            raise ValueError("Expected exactly one news option in the existing Type property.")
        # Querying reads pages; this POST does not create a record.
        result = await request_json(env, f"{NOTION_DATABASES_URL}/{DATABASE_ID}/query", "POST", {
            "page_size": 1,
            "sorts": [{"property": "Date", "direction": "descending"}],
            "filter": {"and": [
                {"property": "Type", "select": {"equals": news_options[0]}},
                {"property": "Summary", "rich_text": {"is_empty": True}},
                {"property": "Summarized At", "date": {"is_empty": True}},
                {"or": [
                    {"property": "Summary Status", "select": {"is_empty": True}},
                    {"property": "Summary Status", "select": {"equals": "Pending"}},
                ]},
            ]},
        })
        if not result.get("results"):
            raise ValueError("No untouched news page is available. Nothing was changed.")
        page_id = result["results"][0]["id"]

    page_id = str(UUID(str(page_id)))
    url = f"{NOTION_PAGES_URL}/{page_id}"
    before = await request_json(env, url)
    verify_target(before)
    try:
        await update_story_summary(env, page_id, pending_summary_properties())
    except Exception:
        # A failed response does not prove the write failed. Read back instead
        # of blindly repeating it. No summary generation occurs in this script.
        print("Update returned an error; checking whether the requested state was saved.")
    after = await request_json(env, url)
    verify_update(before, after)
    print(f"Existing news page verified as Pending: {page_id}")
    print("All eight original properties are unchanged. No new page was created.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mark-pending", action="store_true")
    parser.add_argument("--page-id", help="Optional news page ID; otherwise find an untouched page.")
    args = parser.parse_args()
    asyncio.run(main(args.mark_pending, args.page_id))
