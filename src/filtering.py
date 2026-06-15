AI_KEYWORDS = [
    "ai",
    "artificial intelligence",
    "llm",
    "large language model",
    "machine learning",
    "deep learning",
    "neural",
    "openai",
    "anthropic",
    "google deepmind",
    "deepmind",
    "hugging face",
    "model",
    "inference",
    "agent",
    "reasoning",
    "transformer",
    "diffusion",
    "fine-tuning",
    "training",
]


def is_ai_related_text(*parts):
    text = " ".join(part for part in parts if part).lower()
    return any(keyword in text for keyword in AI_KEYWORDS)
