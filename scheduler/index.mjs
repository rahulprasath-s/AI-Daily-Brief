const berlinClock = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function morningDispatch(scheduledTime) {
  const instant = new Date(scheduledTime);
  const parts = Object.fromEntries(berlinClock.formatToParts(instant)
    .map(({ type, value }) => [type, value]));
  if (parts.hour !== "07" || !["55", "58"].includes(parts.minute)) return null;
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  const releaseAt = new Date(instant.getTime() + (60 - Number(parts.minute)) * 60_000);
  return { date, release_at: releaseAt.toISOString() };
}

async function github(env, path, options = {}) {
  if (!env.GITHUB_WORKFLOW_TOKEN) throw new Error("Missing GITHUB_WORKFLOW_TOKEN");
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GITHUB_WORKFLOW_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "AI-Daily-Brief-Scheduler",
      "Content-Type": "application/json",
    },
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`GitHub request failed: HTTP ${response.status}`);
  return response;
}

export async function dispatchMorning(env, inputs) {
  const workflow = `/actions/workflows/${encodeURIComponent(env.GITHUB_WORKFLOW)}`;
  const runs = await (await github(env, `${workflow}/runs?event=workflow_dispatch&per_page=30`)).json();
  // 07:58 is a recovery attempt, not a second daily publish. A successful or
  // queued 07:55 run is left alone; failed/cancelled runs may be retried.
  const existing = runs.workflow_runs?.find(run =>
    run.display_title === `Morning news · ${inputs.date}` &&
    (run.status !== "completed" || run.conclusion === "success"));
  if (existing) return { dispatched: false, reason: "already_dispatched", run_id: existing.id };
  await github(env, `${workflow}/dispatches`, {
    method: "POST", body: JSON.stringify({ ref: "main", inputs }),
  });
  return { dispatched: true, date: inputs.date, release_at: inputs.release_at };
}

export default {
  async scheduled(controller, env) {
    // Use the scheduled timestamp, not execution time, for DST and date logic.
    const inputs = morningDispatch(controller.scheduledTime);
    if (!inputs) return;
    const result = await dispatchMorning(env, inputs);
    console.log(JSON.stringify({ event: "morning_dispatch", ...result }));
  },
  async fetch(request, env) {
    if (request.method !== "GET" || new URL(request.url).pathname !== "/health") {
      return new Response("Not found", { status: 404 });
    }
    return Response.json({
      ok: Boolean(env.GITHUB_WORKFLOW_TOKEN), scheduler: "cloudflare_cron",
      prepare_at: "07:55", retry_at: "07:58", release_at: "08:00",
      timezone: "Europe/Berlin",
    });
  },
};
