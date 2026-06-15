import json
import traceback

from workers import WorkerEntrypoint, Response

from digest import build_digest
from nim import add_nim_summaries
from notion import filter_missing_digest_items, publish_digest_to_notion
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


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env=None, ctx=None):
        digest = await build_digest(self.env)
        missing_digest = await filter_missing_digest_items(self.env, digest)
        missing_digest = await add_nim_summaries(self.env, missing_digest)
        publish_result = await publish_digest_to_notion(self.env, missing_digest)
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
            })

        if method == "GET" and url.endswith("/status"):
            return json_response({
                "ok": True,
                "service": "ai-daily-brief-worker",
                "daily_cron_utc": "0 6 * * *",
                "publishes_to": "notion",
                "sources": ["arXiv", "hacker news", "hugging face", "techmeme"],
                "summarizer": "qwen/qwen3-next-80b-a3b-instruct via NVIDIA NIM",
                "dedupe_key": "Date + Rank",
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
                digest = await build_digest(self.env)
                missing_digest = await filter_missing_digest_items(self.env, digest)
                missing_digest = await add_nim_summaries(self.env, missing_digest)
                publish_result = await publish_digest_to_notion(self.env, missing_digest)
                return json_response({
                    "ok": True,
                    "message": "Scheduled publish simulation succeeded",
                    "date": digest.get("date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "notion": publish_result,
                    "stats": digest.get("stats", {}),
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
                digest = await build_digest(self.env)
                missing_digest = await filter_missing_digest_items(self.env, digest)
                missing_digest = await add_nim_summaries(self.env, missing_digest)
                publish_result = await publish_digest_to_notion(self.env, missing_digest)
                return json_response({
                    "ok": True,
                    "date": digest.get("date"),
                    "papers": len(digest.get("papers", [])),
                    "news": len(digest.get("news", [])),
                    "missing_papers": len(missing_digest.get("papers", [])),
                    "missing_news": len(missing_digest.get("news", [])),
                    "notion": publish_result,
                    "stats": digest.get("stats", {}),
                })
            except Exception as exc:
                return json_response({
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }, status=500)

        return json_response({"ok": False, "error": "Not found"}, status=404)
