import json

from workers import fetch


NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_NIM_MODEL = "qwen/qwen3-next-80b-a3b-instruct"


def _shorten(text, max_chars=1200):
    text = " ".join(str(text or "").split())
    return text[:max_chars]


def _chat_payload(messages, max_tokens=180):
    return {
        "model": NVIDIA_NIM_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }


async def _chat_completion(nim_api_key, payload):
    url = f"{NVIDIA_NIM_BASE_URL}/chat/completions"
    response = await fetch(
        url,
        method="POST",
        headers={
            "authorization": f"Bearer {nim_api_key}",
            "content-type": "application/json",
        },
        body=json.dumps(payload),
    )

    response_text = await response.text()

    try:
        data = json.loads(response_text)
    except Exception:
        raise RuntimeError(
            f"NVIDIA NIM returned non-JSON response from {url} "
            f"with status {response.status}: {response_text[:500]}"
        )

    if not response.ok:
        raise RuntimeError(f"NVIDIA NIM request failed: {data}")

    return data["choices"][0]["message"]["content"]


def _prompt_for_item(item):
    item_type = item.get("type", "news")
    title = item.get("title", "")
    source = item.get("source", "")
    content = _shorten(item.get("summary_input", "") or title)

    if item_type == "paper":
        instruction = "Summarize this AI research paper."
    else:
        instruction = "Summarize this AI news/model item."

    return f"""
{instruction}

Return only valid JSON with this exact shape:
{{"summary":"one concise summary under 100 words"}}

Title: {title}
Source: {source}
Content: {content}
""".strip()


async def _summarize_item(nim_api_key, item):
    content = await _chat_completion(
        nim_api_key,
        _chat_payload([
            {
                "role": "system",
                "content": "You write factual, concise AI brief summaries. Return only valid JSON.",
            },
            {
                "role": "user",
                "content": _prompt_for_item(item),
            },
        ]),
    )

    try:
        parsed = json.loads(content)
        summary = parsed.get("summary", "")
    except Exception:
        summary = content

    return " ".join(summary.split())


def _ranking_prompt(items, limit):
    candidates = []

    for index, item in enumerate(items):
        candidates.append({
            "id": index,
            "title": item.get("title"),
            "source": item.get("source"),
            "url": item.get("url"),
            "score_hint": item.get("score_hint", 0),
            "context": _shorten(item.get("summary_input", ""), 300),
        })

    return f"""
Rank these AI news/model candidates for a daily AI brief.

Prefer: major AI releases, credible sources, meaningful research/product impact, originality, and broad relevance.
Avoid: duplicates, low-signal model variants, and obscure items unless technically important.

Return only valid JSON with this shape:
{{"ranked_ids":[0,1,2]}}

Return exactly {limit} ids when possible.

Candidates:
{json.dumps(candidates)}
""".strip()


def _apply_rank(items, prefix, limit):
    ranked = items[:limit]

    for index, item in enumerate(ranked, start=1):
        item["rank"] = f"{prefix}{index}"

    return ranked


async def rank_news_with_nim(env, news_items, limit=10):
    nim_api_key = getattr(env, "NVIDIA_NIM_API_KEY", None)

    if not nim_api_key or not news_items:
        return _apply_rank(news_items, "N", limit)

    try:
        content = await _chat_completion(
            nim_api_key,
            _chat_payload([
                {
                    "role": "system",
                    "content": "You rank AI news for a concise daily brief. Return only valid JSON.",
                },
                {
                    "role": "user",
                    "content": _ranking_prompt(news_items, limit),
                },
            ], max_tokens=220),
        )
        ranked_ids = json.loads(content).get("ranked_ids", [])
        ranked_items = [
            news_items[item_id]
            for item_id in ranked_ids
            if isinstance(item_id, int) and 0 <= item_id < len(news_items)
        ]
    except Exception:
        ranked_items = []

    seen_urls = set()
    unique_ranked = []

    for item in ranked_items:
        url = item.get("url")

        if url in seen_urls:
            continue

        seen_urls.add(url)
        unique_ranked.append(item)

    for item in news_items:
        if len(unique_ranked) >= limit:
            break

        url = item.get("url")

        if url in seen_urls:
            continue

        seen_urls.add(url)
        unique_ranked.append(item)

    return _apply_rank(unique_ranked, "N", limit)


async def add_nim_summaries(env, digest):
    nim_api_key = getattr(env, "NVIDIA_NIM_API_KEY", None)

    if not nim_api_key:
        raise RuntimeError("Missing NVIDIA_NIM_API_KEY")

    items = [*digest.get("papers", []), *digest.get("news", [])]

    for item in items:
        item["summary"] = await _summarize_item(nim_api_key, item)

    return digest
