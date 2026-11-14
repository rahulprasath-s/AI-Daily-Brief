"""Enrich existing news pages. Default is extraction-only; --publish enables NIM and Notion writes.

The morning scheduler, ranking, Description, Status, and record creation are untouched.
Run again with the same date to resume durable results awaiting a Notion write.
"""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx

from article_content import ArticleContent, SourceUnavailable, extract_source
from check_story_summary import DATABASE_ID, ORIGINAL_TYPES, load_config, request_json, verify_schema
from nim_article_summary import DEFAULT_ARTICLE_MODEL, NimArticleClient, retry_after_seconds, source_attribution, summarize_source
from notion import NOTION_DATABASES_URL, NOTION_PAGES_URL, NotionSummaryUpdateError, update_story_summary
from story_summary import ready_summary_properties
from summary_cache import SummaryCache, cache_key


class NotionWritePending(RuntimeError):
    pass


def plain_text(property_value):
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in property_value.get("rich_text", property_value.get("title", [])))


def result_properties(status, reason):
    return {
        "Summary": {"rich_text": [{"type": "text", "text": {"content": reason[:1500]}}]},
        "Summary Status": {"select": {"name": status}},
        "Summarized At": {"date": None},
    }


def matches_saved(properties, expected):
    actual_status = (properties["Summary Status"].get("select") or {}).get("name")
    actual_date = properties["Summarized At"].get("date")
    expected_date = expected["Summarized At"]["date"]
    if actual_date and expected_date:
        actual_time = datetime.fromisoformat(actual_date["start"].replace("Z", "+00:00"))
        expected_time = datetime.fromisoformat(expected_date["start"].replace("Z", "+00:00"))
        # Accept Notion's minute-normalized representation, including older
        # checkpoints that recorded seconds. Text and status must still match.
        dates_equal = actual_time.replace(second=0, microsecond=0) == expected_time.replace(second=0, microsecond=0)
    else:
        dates_equal = actual_date is None and expected_date is None
    return (
        actual_status == expected["Summary Status"]["select"]["name"]
        and plain_text(properties["Summary"]) == plain_text(expected["Summary"])
        and dates_equal
    )


async def write_checkpoint(env, page, properties, cache, identity):
    page_id = page["id"]
    key = f"pending_write:{page_id}"
    if matches_saved(page["properties"], properties):
        cache.delete(key)
        return page
    # Commit generated text before any network write. Retrying never needs NIM.
    cache.set(key, {"identity": identity, "properties": properties})
    for attempt in range(4):
        error = None
        try:
            await update_story_summary(env, page_id, properties)
        except Exception as exc:
            error = exc
        try:
            saved = await request_json(env, f"{NOTION_PAGES_URL}/{page_id}")
            for name in ORIGINAL_TYPES:
                if saved["properties"][name] != page["properties"][name]:
                    raise NotionWritePending(f"Original field changed concurrently: {name}; inspect before retrying.")
            if matches_saved(saved["properties"], properties):
                cache.delete(key)
                return saved
        except NotionWritePending:
            raise
        except Exception:
            # Verification failures retain the checkpoint for a later run.
            pass
        if isinstance(error, NotionSummaryUpdateError) and error.status not in (429, 500, 502, 503, 504):
            break
        if attempt < 3:
            delay = retry_after_seconds(getattr(error, "retry_after", None))
            await asyncio.sleep(delay if delay is not None else 2 ** attempt)
    raise NotionWritePending("Notion write is unverified; generated result is cached for retry without another NIM call.")


async def fetch_pages(env, target_date):
    pages, cursor = [], None
    while True:
        payload = {"page_size": 100, "filter": {"property": "Date", "date": {"equals": target_date}}}
        if cursor:
            payload["start_cursor"] = cursor
        data = await request_json(env, f"{NOTION_DATABASES_URL}/{DATABASE_ID}/query", "POST", payload)
        pages.extend(data.get("results", []))
        cursor = data.get("next_cursor") if data.get("has_more") else None
        if not cursor:
            break
    ranked = []
    for page in pages:
        p = page["properties"]
        rank = plain_text(p["Rank"])
        kind = (p["Type"].get("select") or {}).get("name", "").casefold()
        if kind == "news" and rank in {f"N{i}" for i in range(1, 11)} and not page.get("in_trash"):
            ranked.append(page)
    if len({plain_text(p["properties"]["Rank"]) for p in ranked}) != len(ranked):
        raise ValueError("Duplicate ranks exist for this date; resolve them before enrichment.")
    return sorted(ranked, key=lambda page: int(plain_text(page["properties"]["Rank"])[1:]))


async def run(target_date, publish=False, reuse_sources=False, min_interval=6, budget=1800, model=None):
    date.fromisoformat(target_date)
    env = load_config()
    verify_schema(await request_json(env, f"{NOTION_DATABASES_URL}/{DATABASE_ID}"))
    pages = await fetch_pages(env, target_date)
    print(f"Found {len(pages)} existing news records for {target_date}; mode={'publish' if publish else 'extraction-only'}", flush=True)
    if publish and not env.NVIDIA_NIM_API_KEY:
        raise ValueError("Missing NVIDIA_NIM_API_KEY; no records changed.")
    cache = SummaryCache(ROOT / ".summary-cache")
    report, deadline = [], time.monotonic() + budget
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5),
            headers={"User-Agent": "AI-Daily-Brief/1.0 (source extraction)"},
        ) as http:
            nim = NimArticleClient(
                http, env.NVIDIA_NIM_API_KEY,
                model=model or env.NVIDIA_NIM_ARTICLE_MODEL or DEFAULT_ARTICLE_MODEL,
                min_interval=min_interval,
            ) if publish else None
            if nim:
                print(f"NIM article model: {nim.model}; minimum request interval: {min_interval}s", flush=True)
            for page in pages:
                p = page["properties"]
                page_id = page["id"]
                url, title, rank = p["Link"].get("url"), plain_text(p["Title"]), plain_text(p["Rank"])
                identity = cache_key(url, title)
                row = {"page_id": page_id, "rank": rank, "url": url}
                print(f"{rank}: extracting/processing source", flush=True)
                if time.monotonic() >= deadline:
                    row.update(status="Pending", reason="Batch time budget reached; rerun to resume.")
                    report.append(row)
                    continue
                try:
                    async with asyncio.timeout(min(900, deadline - time.monotonic())):
                        checkpoint = cache.get(f"pending_write:{page_id}")
                        if publish and checkpoint and checkpoint["identity"] == identity:
                            saved_status = (p["Summary Status"].get("select") or {}).get("name")
                            if saved_status == "Ready":
                                if not matches_saved(p, checkpoint["properties"]):
                                    raise NotionWritePending("A different Ready result exists; refusing to overwrite it.")
                                cache.delete(f"pending_write:{page_id}")
                            else:
                                await write_checkpoint(env, page, checkpoint["properties"], cache, identity)
                            row.update(status=checkpoint["properties"]["Summary Status"]["select"]["name"], resumed_write=True)
                        else:
                            source_key = f"source:{page_id}"
                            snapshot = cache.get(source_key) if reuse_sources else None
                            if snapshot and snapshot.get("identity") == identity:
                                content = ArticleContent(**snapshot["content"])
                            else:
                                content = await extract_source(http, url)
                                cache.set(source_key, {"identity": identity, "content": asdict(content)})
                            row.update(source_kind=content.kind, source_words=len(content.text.split()), source_hash=content.fingerprint)
                            if not publish:
                                row["status"] = "Extracted"
                            else:
                                last_success = cache.get(f"ready:{page_id}")
                                ready = (p["Summary Status"].get("select") or {}).get("name") == "Ready"
                                if ready and (last_success is None or (
                                    last_success["source_hash"] == content.fingerprint
                                    and last_success["text"] == plain_text(p["Summary"])
                                )):
                                    row.update(status="Ready", skipped=True)
                                    # Add missing publisher attribution to a known, cached model-card
                                    # result without regenerating it or altering the eight original fields.
                                    existing_text = plain_text(p["Summary"])
                                    attributed = source_attribution(existing_text, content.kind, content.url)
                                    if last_success and attributed != existing_text:
                                        timestamp = datetime.fromisoformat(p["Summarized At"]["date"]["start"].replace("Z", "+00:00"))
                                        properties = ready_summary_properties(attributed, timestamp)
                                        cache.set(f"ready:{page_id}", {"source_hash": content.fingerprint, "text": attributed})
                                        await write_checkpoint(env, page, properties, cache, identity)
                                        row["attribution_normalized"] = True
                                else:
                                    text = await summarize_source(nim, cache, page_id, title, content)
                                    properties = ready_summary_properties(text, datetime.now(timezone.utc))
                                    cache.set(f"ready:{page_id}", {"source_hash": content.fingerprint, "text": text})
                                    await write_checkpoint(env, page, properties, cache, identity)
                                    row.update(status="Ready", summary_words=len(text.split()))
                except NotionWritePending as exc:
                    row.update(status="Write pending", reason=str(exc))
                except Exception as exc:
                    status = "Unavailable" if isinstance(exc, SourceUnavailable) else "Failed"
                    reason = str(exc) or type(exc).__name__
                    row.update(status=status, reason=reason[:500])
                    # An existing Ready summary is preserved if re-extraction fails.
                    existing_ready = (p["Summary Status"].get("select") or {}).get("name") == "Ready"
                    if existing_ready:
                        row.update(status="Ready", preserved_existing_summary=True)
                    if publish and not existing_ready and not cache.get(f"pending_write:{page_id}"):
                        try:
                            await write_checkpoint(env, page, result_properties(status, reason), cache, identity)
                        except Exception as write_error:
                            row["write_error"] = str(write_error)[:300]
                report.append(row)
                print(json.dumps(row), flush=True)
            result = {"date": target_date, "mode": "publish" if publish else "extraction-only",
                      "nim_requests": nim.requests if nim else 0,
                      "counts": dict(Counter(row["status"] for row in report)), "stories": report}
            path = ROOT / ".summary-cache" / f"report-{target_date}.json"
            path.write_text(json.dumps(result, indent=2))
            print(f"Report saved: {path}", flush=True)
            print(json.dumps({"counts": result["counts"], "nim_requests": result["nim_requests"]}), flush=True)
            return result
    finally:
        cache.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat())
    parser.add_argument("--publish", action="store_true", help="Enable live NIM calls and summary-only Notion updates.")
    parser.add_argument("--reuse-sources", action="store_true", help="Use explicitly cached extraction snapshots from a preflight run.")
    parser.add_argument("--min-interval", type=float, default=6)
    parser.add_argument("--model", help="NIM article model; defaults to NVIDIA_NIM_ARTICLE_MODEL or the available instruction-model default.")
    args = parser.parse_args()
    result = asyncio.run(run(args.date, args.publish, args.reuse_sources, args.min_interval, model=args.model))
    if args.publish and any(row["status"] in ("Failed", "Write pending", "Pending") for row in result["stories"]):
        raise SystemExit(1)
