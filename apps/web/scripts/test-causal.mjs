// Offline contracts only: fetch is replaced before any client operation. No provider calls.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";

const read = async (path) => fs.readFile(new URL(`../src/${path}`, import.meta.url), "utf8");
const compile = (source) => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const url = (source) => `data:text/javascript;base64,${Buffer.from(compile(source)).toString("base64")}`;
const clientUrl = url(await read("api/client.ts"));
const sessionUrl = url(await read("api/session.ts"));
const { ApiError } = await import(clientUrl);
const session = await import(sessionUrl);
const { createHttpClient } = await import(url((await read("api/http.ts")).replace('from "./client"', `from "${clientUrl}"`).replace('from "./session"', `from "${sessionUrl}"`)));
const { causalOutcome, policyEffectLabel, causalCost, mediaFailure, recordedValue, incompleteComparison, causalFailureMessage, causalFailure, causalJobLabel } = await import(url(await read("lib/causal.ts")));
const { buildWorkflow } = await import(url(await read("lib/workflow.ts")));
const client = createHttpClient();
let checked = 0;
const check = async (name, callback) => { await callback(); process.stdout.write(`ok ${++checked} - ${name}\n`); };
const originalFetch = globalThis.fetch;
const recorded = { status: "completed", plan_only: false, decision: "run_experiments", verdicts: [] };
try {
  await check("completed is not an improvement; plan-only and do-not-edit remain separate", () => {
    assert.equal(causalOutcome(recorded), "No comparison recorded");
    assert.equal(causalOutcome({ ...recorded, plan_only: true }), "Plan only · no edit tested");
    assert.equal(causalOutcome({ ...recorded, plan_only: true, decision: "do_not_edit" }), "No defensible edit");
  });
  await check("failures and cancellation take precedence over earlier winning arms", () => {
    assert.equal(causalOutcome({ ...recorded, status: "failed", verdicts: ["win"] }), "Stopped before completion");
    assert.equal(causalOutcome({ ...recorded, status: "cancelled", verdicts: ["win"] }), "Canceled");
    assert.match(causalOutcome({ ...recorded, verdicts: ["incomplete"] }), /incomplete/);
    assert.equal(causalOutcome({ ...recorded, verdicts: ["loss", "neutral"] }), "No tested revision won");
  });
  await check("a recorded winner is labeled as a model comparison", () => assert.equal(causalOutcome({ ...recorded, verdicts: ["win", "loss"] }), "A revision won the model comparison"));
  await check("a historical loss cannot conceal an incomplete comparison", () => {
    const arm = { verdict: "loss", evaluation_complete: null, comparison: { outcome: "insufficient_evidence" } };
    assert.equal(incompleteComparison(arm), true);
    assert.equal(causalOutcome({ status: "completed", config: { plan_only: false }, plan: { decision: "run_experiments" }, arms: [arm] }), "A comparison has insufficient evidence");
    assert.equal(causalOutcome({ status: "completed", config: { plan_only: false }, plan: { decision: "run_experiments" }, arms: [{ ...arm, verdict: "win" }] }), "A comparison has insufficient evidence");
    assert.match(causalFailureMessage("ProviderError: credit_balance_exhausted"), /Add credits/);
  });
  await check("the exact worker wrapper cannot mask the saved sanitized credit failure", () => {
    const failure = causalFailure({ jobError: "RuntimeError: operation failed; inspect saved job stages", runError: "ProviderError: provider credits exhausted; no automatic retry" });
    assert.equal(failure.title, "Credits exhausted");
    assert.match(failure.message, /configured provider account/);
    assert.equal(failure.details["Job error"], "RuntimeError: operation failed; inspect saved job stages");
    assert.equal(failure.details["Run error"], "ProviderError: provider credits exhausted; no automatic retry");
  });
  await check("known stop-reason and terminal-event causes survive generic record errors", () => {
    const generic = "RuntimeError: operation failed; inspect saved job stages";
    assert.equal(causalFailure({jobError: generic, runError: generic, runFailed: true, stopReason: "the run stopped with an error: provider credits exhausted; no automatic retry"}).title, "Credits exhausted");
    assert.equal(causalFailure({jobError: generic, events: [{stage: "FAILED", message: "provider credits exhausted", data: null}]}).title, "Credits exhausted");
  });
  await check("a generic stopping reason cannot mask the saved terminal credit failure", () => {
    const generic = "RuntimeError: operation failed; inspect saved job stages";
    const event = { stage: "FAILED", message: "provider credits exhausted; no automatic retry", data: null };
    for (const stopReason of ["the run stopped with an error", "the run stopped with an error.", `the run stopped with an error: ${generic}`]) {
      const failure = causalFailure({ runError: generic, jobError: generic, runFailed: true, stopReason, events: [event] });
      assert.equal(failure.title, "Credits exhausted");
      assert.equal(failure.details["Stop reason"], stopReason);
      assert.equal(failure.details["Terminal failure event"], event.message);
    }
  });
  await check("nonfailure content and ordinary rate limits do not become credit exhaustion", () => {
    assert.equal(causalFailure({runFailed: false, stopReason: "no credits remaining", events: [{stage: "PLANNING", message: "no credits remaining", data: null}]}), null);
    assert.equal(causalFailure({runError: "429 rate_limit_exceeded; try later"}).title, "Session stopped");
    assert.equal(causalFailure({runError: "Invalid source media", jobError: "operation failed"}).details["Run error"], "Invalid source media");
  });
  await check("job progress removes duplicated terminal status but preserves distinct phases", () => {
    assert.equal(causalJobLabel("FAILED", "FAILED"), "Failed");
    assert.equal(causalJobLabel("RUNNING", "COLD_REVIEW"), "Running · Cold review");
    assert.equal(causalJobLabel(), "Connecting");
  });
  await check("score movement cannot be presented as a changed selection", () => {
    const effect = { score_changes: ["X1"], order_changed: false, selection_changed: false, first_choice_changed: false };
    assert.equal(policyEffectLabel(effect), "Scores changed; the choice did not");
    assert.equal(policyEffectLabel({ ...effect, order_changed: true }), "Memory changed the ranking");
    assert.equal(policyEffectLabel({ ...effect, first_choice_changed: true }), "Memory changed the first experiment");
  });
  await check("unknown cost and missing evaluation flags never become zero or false", () => {
    assert.equal(causalCost(null), "Unknown"); assert.equal(causalCost(undefined), "Unknown"); assert.equal(causalCost(0), "$0.0000");
    assert.equal(recordedValue(null), "Not recorded"); assert.equal(recordedValue(false), "No");
  });
  await check("missing media and changed source bytes have distinct explanations", () => {
    assert.equal(mediaFailure(404).title, "Media file missing");
    assert.equal(mediaFailure(409).title, "Source file changed");
    assert.match(mediaFailure(409).detail, /recorded video hash/);
  });
  await check("fresh launch calls the causal API with its own IDs and no saved audit", async () => {
    const body = { video_id: "upl-neutral", objective: "Preserve comprehension", constraints: ["Keep words"], arms: 2, plan_only: false, max_model_calls: 120, deadline_s: 1800, idempotency_key: "deliberate-launch" };
    globalThis.fetch = async (path, options) => {
      assert.equal(path, "/api/causal"); assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), body); assert.equal(JSON.parse(options.body).audit_id, undefined);
      return Response.json({ job_id: "job-real", causal_id: "causal-real", created: true });
    };
    assert.deepEqual(await client.startCausal(body), { job_id: "job-real", causal_id: "causal-real", created: true });
  });
  await check("saved audit reuse and idempotency conflict remain explicit", async () => {
    globalThis.fetch = async (_path, options) => {
      assert.equal(JSON.parse(options.body).audit_id, "audit-saved");
      return Response.json({ detail: "key used for another request" }, { status: 409 });
    };
    await assert.rejects(client.startCausal({ audit_id: "audit-saved", idempotency_key: "same-key" }), (error) => error instanceof ApiError && error.status === 409);
  });
  await check("queued detail 404 and admission 403/422 preserve status for the UI", async () => {
    for (const status of [404, 403, 422]) {
      globalThis.fetch = async () => Response.json({ detail: "contract rejection" }, { status });
      await assert.rejects(client.getCausal("pending"), (error) => error instanceof ApiError && error.status === status);
    }
  });
  await check("filtered history encodes the source identity", async () => {
    globalThis.fetch = async (path) => { assert.equal(path, "/api/causal?video_id=video%20one"); return Response.json([]); };
    assert.deepEqual(await client.listCausal("video one"), []);
  });
  await check("SSE resume parses real stage updates and merges by ID", async () => {
    const stage = { id: "x1-render", key: "render", title: "Render X1", status: "running" };
    const events = [{ seq: 8, stage: "WORKFLOW_STAGE", data: { stage } }, { seq: 9, stage: "WORKFLOW_STAGE", data: { stage: { ...stage, status: "completed", duration_ms: 412 } } }];
    const wire = events.map((event) => `event: message\ndata: ${JSON.stringify(event)}\n\n`).join("") + "event: end\ndata: {}\n\n";
    globalThis.fetch = async (path) => {
      assert.equal(path, "/api/jobs/job-real/events?after=7");
      return new Response(new ReadableStream({ start(controller) { const bytes = new TextEncoder().encode(wire); controller.enqueue(bytes.slice(0, 93)); controller.enqueue(bytes.slice(93)); controller.close(); } }));
    };
    const received = [];
    await client.streamJobEvents("job-real", 7, (event) => received.push(event), new AbortController().signal);
    assert.deepEqual(received, events);
    const merged = buildWorkflow([], received);
    assert.equal(merged.stages.length, 1); assert.equal(merged.stages[0].duration_ms, 412); assert.equal(merged.stages[0].status, "completed");
  });
  await check("polling fallback and cancel use the original job ID", async () => {
    const calls = [];
    globalThis.fetch = async (path, options) => { calls.push([path, options?.method]); return Response.json(path.endsWith("cancel") ? { id: "job-real", cancel_requested: true } : []); };
    await client.getJobEvents("job-real", 9); await client.cancelJob("job-real");
    assert.deepEqual(calls, [["/api/jobs/job-real/events.json?after=9", undefined], ["/api/jobs/job-real/cancel", "POST"]]);
  });
  await check("401 locks the UI, and transient credential exchange unlocks only on an authenticated receipt", async () => {
    globalThis.fetch = async () => Response.json({ detail: "authentication required" }, { status: 401 });
    await assert.rejects(client.getHealth()); assert.equal(session.sessionIsLocked(), true);
    globalThis.fetch = async (path, options) => { assert.equal(path, "/api/session"); assert.equal(options.headers.Authorization, "Bearer synthetic-test-token"); assert.equal(options.body, undefined); return Response.json({ authenticated: true, expires_in_seconds: 43200 }); };
    await session.unlockSession("synthetic-test-token"); assert.equal(session.sessionIsLocked(), false);
    session.markSessionLocked(); globalThis.fetch = async () => Response.json({ authenticated: false });
    await assert.rejects(session.unlockSession("synthetic-test-token")); assert.equal(session.sessionIsLocked(), true);
  });
} finally { globalThis.fetch = originalFetch; }
process.stdout.write(`${checked} causal and session contract checks passed.\n`);
