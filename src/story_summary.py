from enum import StrEnum
from datetime import datetime

class SummaryStatus(StrEnum):
    PENDING = "Pending"
    READY = "Ready"
    UNAVAILABLE = "Unavailable"
    FAILED = "Failed"


def pending_summary_properties():
    """Build initial summary properties for a newly created news page."""
    return {
        "Summary": {"rich_text": []},
        "Summary Status": {
            "select": {"name": SummaryStatus.PENDING.value},
        },
        "Summarized At": {"date": None},
    }

def ready_summary_properties(text: str, summarized_at: datetime):
    """Build a completed-summary update without touching existing fields."""
    text = text.strip()

    if not text:
        raise ValueError("A Ready summary cannot be empty.")

    if summarized_at.tzinfo is None or summarized_at.utcoffset() is None:
        raise ValueError("summarized_at must include a timezone.")

    # Split into Notion-safe rich-text items without losing content.
    chunks = [
        text[index:index + 2000]
        for index in range(0, len(text), 2000)
    ]

    if len(chunks) > 100:
        raise ValueError("Summary exceeds the rich-text array limit.")

    return {
        "Summary": {
            "rich_text": [
                {"type": "text", "text": {"content": chunk}}
                for chunk in chunks
            ],
        },
        "Summary Status": {
            "select": {"name": SummaryStatus.READY.value},
        },
        "Summarized At": {
            # Notion's saved Date value was observed at minute precision.
            "date": {"start": summarized_at.replace(second=0, microsecond=0).isoformat()},
        },
    }
