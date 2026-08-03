import json
import time as time_module
import traceback
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from workers import WorkerEntrypoint, Response

from digest import build_digest, _news_window
from nim import add_nim_summaries, finalize_news_for_publish
from notion import (
    filter_missing_digest_items,
    get_published_items,
    publish_digest_to_notion,
)
from sources.arxiv import fetch_arxiv_papers
from sources.hackernews import fetch_hackernews_ai_stories
from sources.huggingface import fetch_huggingface_ai_models
from sources.techmeme import fetch_techmeme_ai_stories


def json_response(data, status=200):
    return Response(
        json.dumps(data),
        status=status,
        headers={"content-type": "application/json"},
    )


def _parse_publish_date_override(value, env):
    if value in (None, ""):
        return None

    timezone_name = getattr(env, "DIGEST_TIMEZONE", None) or "UTC"

    try:
        digest_tz = ZoneInfo(timezone_name)
    except Exception:
        digest_tz = timezone.utc

    try:
        target_date = datetime.fromisoformat(str(value)).date()
    except ValueError:
        raise ValueError("Invalid date override. Use YYYY-MM-DD.")

    # Any time during the target publish date yields the same previous-calendar-day window.
    local_datetime = datetime.combine(target_date, time(12, 0), tzinfo=digest_tz)
    return local_datetime.astimezone(timezone.utc)


async def _build_publishable_digest(env, now_utc=None):
    published_items = await get_published_items(env)
    digest = await build_digest(
        env,
        now_utc=now_utc,
        published_items=published_items,
    )
    missing_digest = await filter_missing_digest_items(
        env,
        digest,
        published_items=published_items,
    )
    missing_digest = await add_nim_summaries(env, missing_digest)
    missing_digest = await _prepare_digest_for_publish(missing_digest)
    return digest, missing_digest, published_items


async def _build_publishable_digest_with_timings(env, now_utc=None):
    timings = {}

    started_at = time_module.monotonic()
    published_items = await get_published_items(env)
    timings["load_notion_history_seconds"] = round(
        time_module.monotonic() - started_at,
        3,
    )

    started_at = time_module.monotonic()
    digest = await build_digest(
        env,
        now_utc=now_utc,
        published_items=published_items,
    )
    timings["build_digest_seconds"] = round(
        time_module.monotonic() - started_at,
        3,
    )

    started_at = time_module.monotonic()
    missing_digest = await filter_missing_digest_items(
        env,
        digest,
        published_items=published_items,
    )
    timings["filter_missing_seconds"] = round(
        time_module.monotonic() - started_at,
        3,
    )

    started_at = time_module.monotonic()
    missing_digest = await add_nim_summaries(env, missing_digest)
    timings["add_nim_summaries_seconds"] = round(
        time_module.monotonic() - started_at,
        3,
    )

    started_at = time_module.monotonic()
    missing_digest = await _prepare_digest_for_publish(missing_digest)
    timings["prepare_publish_seconds"] = round(
        time_module.monotonic() - started_at,
        3,
    )

    return digest, missing_digest, published_items, timings


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env=None, ctx=None):
        digest, missing_digest, published_items = await _build_publishable_digest(
            self.env
        )
        publish_result = await publish_digest_to_notion(
            self.env,
            missing_digest,
            published_items=published_items,
        )
        print(json.dumps({
            "event": "daily_digest_published",
            "date": digest.get("date"),
            "papers": len(digest.get("papers", [])),
            "news": len(digest.get("news", [])),
            "missing_papers": len(missing_digest.get("papers", [])),
            "missing_news": len(missing_digest.get("news", [])),
            "notion_created": publish_result.get("created"),
            "stats": digest.get("stats", {}),
        }))

    async def fetch(self, request):
        url = request.url
        method = request.method

        if method == "GET" and url.endswith("/health"):
            return json_response({
                "ok": True,
                "service": "ai-daily-brief-worker",
            })

        if method == "GET" and url.endswith("/config-check"):
            return json_response({
                "ok": True,
                "secrets": {
                    "NOTION_TOKEN": bool(getattr(self.env, "NOTION_TOKEN", None)),
                    "NOTION_DATABASE_ID": bool(getattr(self.env, "NOTION_DATABASE_ID", None)),
                    "NVIDIA_NIM_API_KEY": bool(getattr(self.env, "NVIDIA_NIM_API_KEY", None)),
                },
                "config": {
                    "DIGEST_TIMEZONE": getattr(self.env, "DIGEST_TIMEZONE", None) or "UTC",
                    "DIGEST_WINDOW_MODE": "previous_calendar_day",
                },
            })

        if method == "GET" and url.endswith("/status"):
            news_window = _news_window(self.env)
            return json_response({
                "ok": True,
                "service": "ai-daily-brief-worker",
                "daily_cron_utc": "0 6 * * *",
                "reconciliation_cron_utc": "0 12 * * *",
                "publishes_to": "notion",
                "sources": ["arXiv", "hacker news", "hugging face", "techmeme"],
                "summarizer": "qwen/qwen3-next-80b-a3b-instruct via NVIDIA NIM",
                "dedupe_key": "Date + Rank, plus all-history Link, Title, and Summary checks",
                "news_window": {
                    "timezone": news_window["timezone"],
                    "kind": news_window["window_kind"],
                    "current_window_start": news_window["window_start_local"].isoformat(),
                    "current_window_end": news_window["window_end_local"].isoformat(),
                },
                "secrets_ready": {
                    "NOTION_TOKEN": bool(getattr(self.env, "NOTION_TOKEN", None)),
                    "NOTION_DATABASE_ID": bool(getattr(self.env, "NOTION_DATABASE_ID", None)),
                    "NVIDIA_NIM_API_KEY": bool(getattr(self.env, "NVIDIA_NIM_API_KEY", None)),
                },
            })

        if method == "GET" and url.endswith("/debug/arxiv"):
            papers = await fetch_arxiv_papers(limit=5)
            return json_response({
                "ok": True,
                "count": len(papers),
                "papers": papers,
            })

        if method == "GET" and url.endswith("/debug/hackernews"):
            stories = await fetch_hackernews_ai_stories(limit=10)
            return json_response({
                "ok": True,
                "count": len(stories),
                "news": stories,
            })

        if method == "GET" and url.endswith("/debug/huggingface"):
            models = await fetch_huggingface_ai_models(limit=10)
            return json_response({
                "ok": True,
                "count": len(models),
                "news": models,
            })

        if method == "GET" and url.endswith("/debug/techmeme"):
            stories = await fetch_techmeme_ai_stories(limit=10)
            return json_response({
                "ok": True,
                "count": len(stories),
                "news": stories,
            })

        if method == "POST" and url.endswith("/debug/nim"):
            try:
                digest = await build_digest(self.env)
                sample_digest = {
                    **digest,
                    "papers": digest.get("papers", [])[:1],
                    "news": [],
                }
                summarized = await add_nim_summaries(self.env, sample_digest)
                return json_response({
                    "ok": True,
                    "item": summarized["papers"][0] if summarized["papers"] else None,
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        if method == "POST" and url.endswith("/debug/scheduled"):
            try:
                digest, missing_digest, published_items, timings = (
                    await _build_publishable_digest_with_timings(self.env)
                )
                publish_started_at = time_module.monotonic()
                publish_result = await publish_digest_to_notion(
                    self.env,
                    missing_digest,
                    published_items=published_items,
                )
                timings["publish_to_notion_seconds"] = round(
                    time_module.monotonic() - publish_started_at,
                    3,
                )
                return json_response({
                    "ok": True,
                    "message": "Scheduled publish simulation succeeded",
                    "date": digest.get("date"),
                    "content_date": digest.get("content_date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "notion": publish_result,
                    "stats": digest.get("stats", {}),
                    "timings": timings,
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        if method == "POST" and url.endswith("/run-digest"):
            digest = await build_digest(self.env)
            return json_response(digest)

        if method == "POST" and url.endswith("/publish-digest"):
            try:
                digest, missing_digest, published_items, timings = (
                    await _build_publishable_digest_with_timings(self.env)
                )
                publish_started_at = time_module.monotonic()
                publish_result = await publish_digest_to_notion(
                    self.env,
                    missing_digest,
                    published_items=published_items,
                )
                timings["publish_to_notion_seconds"] = round(
                    time_module.monotonic() - publish_started_at,
                    3,
                )
                return json_response({
                    "ok": True,
                    "date": digest.get("date"),
                    "content_date": digest.get("content_date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "notion": publish_result,
                    "stats": digest.get("stats", {}),
                    "timings": timings,
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        if method == "POST" and url.endswith("/debug/publish-date"):
            try:
                body = await request.json()
                raw_date = body.get("date") if isinstance(body, dict) else None
                now_utc = _parse_publish_date_override(raw_date, self.env)
                digest, missing_digest, published_items, timings = (
                    await _build_publishable_digest_with_timings(
                        self.env,
                        now_utc=now_utc,
                    )
                )
                publish_started_at = time_module.monotonic()
                publish_result = await publish_digest_to_notion(
                    self.env,
                    missing_digest,
                    published_items=published_items,
                )
                timings["publish_to_notion_seconds"] = round(
                    time_module.monotonic() - publish_started_at,
                    3,
                )
                return json_response({
                    "ok": True,
                    "message": "One-time publish succeeded",
                    "date": digest.get("date"),
                    "content_date": digest.get("content_date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "notion": publish_result,
                    "stats": digest.get("stats", {}),
                    "timings": timings,
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        if method == "POST" and url.endswith("/debug/build-date"):
            try:
                body = await request.json()
                raw_date = body.get("date") if isinstance(body, dict) else None
                now_utc = _parse_publish_date_override(raw_date, self.env)
                digest, missing_digest, _published_items, timings = (
                    await _build_publishable_digest_with_timings(
                        self.env,
                        now_utc=now_utc,
                    )
                )
                return json_response({
                    "ok": True,
                    "message": "Digest build succeeded",
                    "date": digest.get("date"),
                    "content_date": digest.get("content_date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "stats": digest.get("stats", {}),
                    "timings": timings,
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        return json_response({"ok": False, "error": "Not found"}, status=404)


async def _prepare_digest_for_publish(digest):
    return finalize_news_for_publish(digest)
