"""Durable local checkpoints, including results awaiting a Notion write."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def cache_key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SummaryCache:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "results.sqlite3"
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, payload TEXT NOT NULL, saved_at TEXT NOT NULL)"
        )
        self.connection.commit()

    def get(self, key):
        row = self.connection.execute("SELECT payload FROM results WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def delete(self, key):
        with self.connection:
            self.connection.execute("DELETE FROM results WHERE key = ?", (key,))

    def close(self):
        self.connection.close()
