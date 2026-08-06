import argparse
import asyncio
import json
import os
import time
import traceback
from types import SimpleNamespace

from digest import build_digest
from nim import add_nim_summaries, finalize_news_for_publish
from notion import (
    filter_missing_digest_items,
    get_published_items,
    publish_digest_to_notion,
)


REQUIRED_ENVIRONMENT_VARIABLES = (
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "NVIDIA_NIM_API_KEY",
)


def load_environment():
    missing = [
        name for name in REQUIRED_ENVIRONMENT_VARIABLES
        if not os.environ.get(name)
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return SimpleNamespace(
        NOTION_TOKEN=os.environ["NOTION_TOKEN"],
        NOTION_DATABASE_ID=os.environ["NOTION_DATABASE_ID"],
        NVIDIA_NIM_API_KEY=os.environ["NVIDIA_NIM_API_KEY"],
        DIGEST_TIMEZONE=os.environ.get("DIGEST_TIMEZONE", "Europe/Berlin"),
        NVIDIA_NIM_TIMEOUT_SECONDS=os.environ.get(
            "NVIDIA_NIM_TIMEOUT_SECONDS",
            "15",
        ),
        NVIDIA_NIM_SUMMARY_CONCURRENCY=os.environ.get(
            "NVIDIA_NIM_SUMMARY_CONCURRENCY",
            "4",
        ),
    )


async def run_digest(env, publish=False):
    timings = {}

    started_at = time.monotonic()
    published_items = await get_published_items(env)
    timings["load_notion_history_seconds"] = round(
        time.monotonic() - started_at,
        3,
    )

    started_at = time.monotonic()
    digest = await build_digest(env, published_items=published_items)
    timings["build_digest_seconds"] = round(time.monotonic() - started_at, 3)

    started_at = time.monotonic()
    missing_digest = await filter_missing_digest_items(
        env,
        digest,
        published_items=published_items,
    )
    timings["filter_missing_seconds"] = round(time.monotonic() - started_at, 3)

    started_at = time.monotonic()
    missing_digest = await add_nim_summaries(env, missing_digest)
    missing_digest = finalize_news_for_publish(missing_digest)
    timings["prepare_publish_seconds"] = round(time.monotonic() - started_at, 3)

    publish_result = None
    if publish:
        started_at = time.monotonic()
        publish_result = await publish_digest_to_notion(
            env,
            missing_digest,
            published_items=published_items,
        )
        timings["publish_to_notion_seconds"] = round(
            time.monotonic() - started_at,
            3,
        )

    return {
        "ok": True,
        "mode": "publish" if publish else "dry-run",
        "date": digest.get("date"),
        "content_date": digest.get("content_date"),
        "papers": len(digest.get("papers", [])),
        "news": len(digest.get("news", [])),
        "missing_papers": len(missing_digest.get("papers", [])),
        "missing_news": len(missing_digest.get("news", [])),
        "notion": publish_result,
        "stats": digest.get("stats", {}),
        "timings": timings,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build the AI Daily Brief and optionally publish it to Notion.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish missing digest rows to Notion. Without this flag, no rows are created.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        result = asyncio.run(run_digest(load_environment(), publish=args.publish))
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }, indent=2))
        raise


if __name__ == "__main__":
    main()
