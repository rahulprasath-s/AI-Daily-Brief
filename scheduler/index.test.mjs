import test from "node:test";
import assert from "node:assert/strict";
import worker, { dispatchMorning, morningDispatch } from "./index.mjs";

test("prepare and retry remain at 07:55/07:58 Berlin across both DST changes", () => {
  for (const [day, utcHour] of [
    ["2026-01-15", 6], ["2026-03-28", 6], ["2026-03-29", 5],
    ["2026-07-15", 5], ["2026-10-24", 5], ["2026-10-25", 6],
  ]) {
    for (const minute of [55, 58]) {
      const event = new Date(`${day}T0${utcHour}:${minute}:00Z`);
      assert.deepEqual(morningDispatch(event), {
        date: day, release_at: `${day}T0${utcHour + 1}:00:00.000Z`,
      });
      const wrongHour = new Date(event.getTime() + (utcHour === 5 ? 1 : -1) * 3_600_000);
      assert.equal(morningDispatch(wrongHour), null);
    }
  }
});

test("off-schedule times do not dispatch", () => {
  assert.equal(morningDispatch(new Date("2026-11-03T07:55:00Z")), null);
  assert.equal(morningDispatch(new Date("2026-11-03T06:56:00Z")), null);
});

const env = {
  GITHUB_WORKFLOW_TOKEN: "test-token",
  GITHUB_REPOSITORY: "owner/repo", GITHUB_WORKFLOW: "daily-brief.yml",
};
const inputs = { date: "2026-11-03", release_at: "2026-11-03T07:00:00.000Z" };

test("dispatch includes the publish date and earliest release timestamp", async t => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, ...options });
    return calls.length === 1 ? Response.json({ workflow_runs: [] }) : new Response(null, { status: 204 });
  });
  assert.equal((await dispatchMorning(env, inputs)).dispatched, true);
  assert.deepEqual(JSON.parse(calls[1].body), { ref: "main", inputs });
  assert.equal(calls[1].method, "POST");
});

test("retry skips queued, running, or successful runs; retries failures", async t => {
  for (const [status, conclusion, dispatch] of [
    ["queued", null, false], ["in_progress", null, false],
    ["completed", "success", false], ["completed", "failure", true],
    ["completed", "cancelled", true],
  ]) {
    let count = 0;
    const mock = t.mock.method(globalThis, "fetch", async () => {
      count++;
      return count === 1 ? Response.json({ workflow_runs: [{
        id: 123, display_title: `Morning news · ${inputs.date}`, status, conclusion,
      }] }) : new Response(null, { status: 204 });
    });
    assert.equal((await dispatchMorning(env, inputs)).dispatched, dispatch);
    assert.equal(count, dispatch ? 2 : 1);
    mock.mock.restore();
  }
});

test("GitHub errors fail the trigger instead of claiming success or logging tokens", async t => {
  t.mock.method(globalThis, "fetch", async () => new Response("private details", { status: 403 }));
  await assert.rejects(dispatchMorning(env, inputs), /^Error: GitHub request failed: HTTP 403$/);
});

test("no public route can trigger a publishing run", async () => {
  const response = await worker.fetch(new Request("https://example.com/dispatch", { method: "POST" }), env);
  assert.equal(response.status, 404);
});
