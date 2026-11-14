const MAX_BYTES = 1_000_000;
const encoder = new TextEncoder();

async function authorized(request, token) {
  const presented = request.headers.get("authorization") || "";
  const hashes = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(presented)),
    crypto.subtle.digest("SHA-256", encoder.encode(`Bearer ${token}`)),
  ]);
  const left = new Uint8Array(hashes[0]), right = new Uint8Array(hashes[1]);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}

function reply(body, status = 200) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

async function boundedJson(request) {
  if (!request.body) throw new Error("Invalid body");
  const reader = request.body.getReader();
  const pieces = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) {
      await reader.cancel();
      throw new RangeError("Object too large");
    }
    pieces.push(value);
  }
  const bytes = new Uint8Array(size);
  let position = 0;
  for (const piece of pieces) { bytes.set(piece, position); position += piece.length; }
  const value = JSON.parse(new TextDecoder().decode(bytes));
  if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("Expected an object");
  return value;
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/health") {
      return reply({ ok: Boolean(env.CACHE && env.SUMMARY_CACHE_TOKEN), backend: "d1" });
    }
    if (!env.CACHE || !env.SUMMARY_CACHE_TOKEN) return reply({ error: "Cache not configured" }, 503);
    if (!await authorized(request, env.SUMMARY_CACHE_TOKEN)) return reply({ error: "Unauthorized" }, 401);
    if (!path.startsWith("/cache/")) return reply({ error: "Not found" }, 404);
    let key;
    try { key = decodeURIComponent(path.slice(7)); } catch { return reply({ error: "Invalid key" }, 400); }
    if (!/^[A-Za-z0-9:_-]{1,200}$/.test(key)) return reply({ error: "Invalid key" }, 400);
    const now = Math.floor(Date.now() / 1000);
    try {
      if (request.method === "GET") {
        const stored = await env.CACHE.prepare(
          "SELECT value FROM summary_cache WHERE key = ? AND expires_at > ?",
        ).bind(key, now).first("value");
        return stored === null ? reply({ found: false }, 404) : reply({ value: JSON.parse(stored) });
      }
      if (request.method === "PUT") {
        let value;
        try { value = await boundedJson(request); }
        catch (error) { return reply({ error: "Invalid or oversized cache object" }, error instanceof RangeError ? 413 : 400); }
        const durable = key.startsWith("pending_write:") || key.startsWith("ready:") || (value.model && value.source_hash);
        const expires = now + (durable ? 365 : 45) * 86400;
        await env.CACHE.prepare(
          "INSERT INTO summary_cache (key, value, expires_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
        ).bind(key, JSON.stringify(value), expires).run();
        await env.CACHE.prepare("DELETE FROM summary_cache WHERE expires_at <= ?").bind(now).run();
        return reply({ saved: true });
      }
      if (request.method === "DELETE") {
        await env.CACHE.prepare("DELETE FROM summary_cache WHERE key = ?").bind(key).run();
        return reply({ deleted: true });
      }
      return reply({ error: "Method not allowed" }, 405);
    } catch {
      // Do not log source text, credentials, or SQL bound values.
      return reply({ error: "Cache temporarily unavailable" }, 503);
    }
  },
};
