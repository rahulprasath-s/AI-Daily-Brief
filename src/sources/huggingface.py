from urllib.parse import urlencode

from js import fetch

from filtering import is_ai_related_text


HUGGING_FACE_MODELS_URL = "https://huggingface.co/api/models"


async def _fetch_json(url):
    response = await fetch(url)

    if not response.ok:
        raise RuntimeError(f"Hugging Face request failed with status {response.status}")

    return (await response.json()).to_py()


def _normalize_model(model):
    model_id = model.get("modelId", "")
    tags = model.get("tags") or []
    downloads = model.get("downloads") or 0
    likes = model.get("likes") or 0

    return {
        "title": model_id,
        "url": f"https://huggingface.co/{model_id}",
        "source": "hugging face",
        "type": "news",
        "published_at": model.get("createdAt"),
        "updated_at": model.get("lastModified"),
        "score_hint": downloads + (likes * 25),
        "downloads": downloads,
        "likes": likes,
        "tags": tags[:12],
        "summary_input": f"{model_id} {' '.join(tags[:12])}",
    }


async def fetch_huggingface_ai_models(limit=10, scan_count=40):
    query = urlencode({
        "sort": "trendingScore",
        "direction": -1,
        "limit": scan_count,
        "full": "true",
    })
    models = await _fetch_json(f"{HUGGING_FACE_MODELS_URL}?{query}")
    ai_models = []

    for model in models:
        normalized = _normalize_model(model)

        if is_ai_related_text(
            normalized["title"],
            " ".join(normalized["tags"]),
            normalized["summary_input"],
        ):
            ai_models.append(normalized)

        if len(ai_models) >= limit:
            break

    return ai_models
