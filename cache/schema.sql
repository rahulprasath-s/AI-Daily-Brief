CREATE TABLE IF NOT EXISTS summary_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS summary_cache_expiration ON summary_cache(expires_at);
