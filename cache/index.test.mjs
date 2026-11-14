import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./index.mjs";

function fixture() {
  const records = new Map();
  const env = { SUMMARY_CACHE_TOKEN: "private-test-token", CACHE: {
    prepare(sql) {
      return { bind(...args) {
        return {
          async first() { const row = records.get(args[0]); return row && row.expires > args[1] ? row.value : null; },
          async run() {
            if (sql.startsWith("INSERT")) records.set(args[0], { value: args[1], expires: args[2] });
            else if (sql.includes("WHERE key")) records.delete(args[0]);
            else for (const [key, row] of records) if (row.expires <= args[0]) records.delete(key);
          },
        };
      } };
    },
  } };
  const call = (method, key, body, token = env.SUMMARY_CACHE_TOKEN) => worker.fetch(new Request(`https://cache.example/cache/${encodeURIComponent(key)}`, {
    method, headers: { authorization: `Bearer ${token}` },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }), env);
  return { call, records, env };
}

test("private objects require authentication", async () => {
  const { call, records } = fixture();
  assert.equal((await call("PUT", "secret", { text: "private" }, "wrong")).status, 401);
  assert.equal(records.size, 0);
});

test("results and pending writes survive separate requests and can be deleted", async () => {
  const { call } = fixture();
  const value = { properties: { Summary: "Generated prose" } };
  assert.equal((await call("PUT", "pending_write:page-id", value)).status, 200);
  assert.deepEqual(await (await call("GET", "pending_write:page-id")).json(), { value });
  assert.equal((await call("DELETE", "pending_write:page-id")).status, 200);
  assert.equal((await call("GET", "pending_write:page-id")).status, 404);
});

test("source snapshots expire earlier than successful results", async () => {
  const { call, records } = fixture();
  await call("PUT", "source:page", { text: "Source" });
  await call("PUT", "ready:page", { text: "Summary" });
  assert.ok(records.get("ready:page").expires - records.get("source:page").expires >= 319 * 86400);
  records.set("old", { value: "{}", expires: 0 });
  await call("PUT", "new", { text: "New" });
  assert.equal(records.has("old"), false);
});

test("reject malformed keys, values, oversized bodies and unsupported methods", async () => {
  const { call } = fixture();
  assert.equal((await call("PUT", "../escape", {})).status, 400);
  assert.equal((await call("PUT", "key", [])).status, 400);
  assert.equal((await call("PUT", "key", { text: "x".repeat(1_000_000) })).status, 413);
  assert.equal((await call("POST", "key", {})).status, 405);
});

test("database failure returns a generic error without payload disclosure", async () => {
  const { call, env } = fixture();
  env.CACHE.prepare = () => { throw new Error("sensitive SQL or payload"); };
  const response = await call("GET", "key");
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "Cache temporarily unavailable" });
});
