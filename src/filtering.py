import re


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


def _keyword_pattern(keyword):
    escaped = re.escape(keyword)
    escaped = escaped.replace(r"\ ", r"[\s\-_]+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


AI_KEYWORD_PATTERNS = [_keyword_pattern(keyword) for keyword in AI_KEYWORDS]


def is_ai_related_text(*parts):
    text = " ".join(str(part) for part in parts if part)
    return any(pattern.search(text) for pattern in AI_KEYWORD_PATTERNS)
