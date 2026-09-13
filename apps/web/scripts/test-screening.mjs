// Spending-route and evidence-label contracts; no network or provider calls.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";
const read = async path => fs.readFile(new URL(`../src/${path}`, import.meta.url), "utf8");
const url = source => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText).toString("base64")}`;
const clientUrl = url(await read("api/client.ts"));
const sessionUrl = url(await read("api/session.ts"));
const { createHttpClient } = await import(url((await read("api/http.ts")).replace('from "./client"', `from "${clientUrl}"`).replace('from "./session"', `from "${sessionUrl}"`)));
const { screeningTitle, screeningRisk, screeningBudget, screeningJobId, screeningJobMatches, screeningCitation } = await import(url(await read("lib/screening.ts")));
const originalFetch = globalThis.fetch;
const client = createHttpClient();
try {
  const requests = [];
  globalThis.fetch = async (path, init) => { requests.push([path, init]); return Response.json({ job_id: "job-real", screen_id: "screen-real", created: true }); };
  await client.startScreening("source-one", "fixed-retry-key");
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], "/api/screenings");
  assert.deepEqual(JSON.parse(requests[0][1].body), { video_id: "source-one", idempotency_key: "fixed-retry-key" });
  globalThis.fetch = async path => { assert.equal(path, "/api/screenings?video_id=a%2Fb"); return Response.json([]); };
  assert.deepEqual(await client.listScreenings("a/b"), []);
  globalThis.fetch = async () => Response.json({ detail: "Screening budget unavailable" }, { status: 503 });
  await assert.rejects(client.startScreening("source-one", "fixed-retry-key"), error => error.status === 503);
  assert.equal(screeningTitle({ status: "complete" }), "Ready for your review");
  assert.equal(screeningTitle({ status: "failed" }), "Screening stopped");
  const moment = { status: "complete", judgment: { attention_risk: "high" }, validation_issues: [], attention_context: "last_endcard" };
  assert.equal(screeningRisk(moment), "Video ending");
  assert.equal(screeningRisk({ ...moment, validation_issues: ["No frame citation"] }), "Check evidence");
  assert.equal(screeningRisk({ ...moment, judgment: null }), "Not reviewed");
  assert.equal(screeningRisk({ ...moment, judgment: null, status: "needs_review", validation_issues: ["Invalid structured response"] }), "Check evidence");
  assert.equal(screeningRisk({ ...moment, attention_context: "content", judgment: { attention_risk: "unknown" } }), "Uncertain");
  assert.equal(screeningBudget(), "Budget unavailable");
  assert.equal(screeningBudget({ available: false, budget: { available_usd: 1 } }), "Screening paused");
  const screenId = "screen_abcdef_123456";
  const jobId = "job_abcdef_123456";
  assert.equal(screeningJobId(screenId, null), jobId);
  assert.equal(screeningJobId(screenId, jobId), jobId);
  assert.equal(screeningJobId(screenId, "job_abcdef_654321"), null);
  assert.equal(screeningJobId(screenId, ""), null);
  assert.equal(screeningJobId("screen_bad/identifier", null), null);
  const job = { id: jobId, kind: "screening", params: { video_id: "source-one" } };
  assert.equal(screeningJobMatches(screenId, job, "source-one"), true);
  assert.equal(screeningJobMatches(screenId, job, "other-source"), false);
  assert.equal(screeningJobMatches(screenId, { ...job, kind: "experiment" }, "source-one"), false);
  assert.equal(screeningJobMatches(screenId, { ...job, id: "job_abcdef_654321" }), false);
  assert.equal(screeningJobMatches(screenId, null), false);
  globalThis.fetch = async (path, init) => {
    assert.equal(path, `/api/jobs/${jobId}/cancel`);
    assert.equal(init.method, "POST");
    return Response.json({ ...job, state: "RUNNING", cancel_requested: true });
  };
  assert.equal((await client.cancelJob(jobId)).cancel_requested, true);
  const evidence = { evidence_frames: [{ t_ms: 0 }, { t_ms: 1999 }], prefix_asr_text: "The container  is closed.\nWe cannot see inside." };
  assert.deepEqual(screeningCitation(evidence, { frame_timestamps_ms: [1999, 1999, 4000, 0.5], asr_quote: "container is closed." }), {
    validTimes: [1999], invalidTimes: [4000, 0.5], quoteValid: true,
  });
  assert.deepEqual(screeningCitation(evidence, { frame_timestamps_ms: [4000], asr_quote: "There is a loaf inside." }), {
    validTimes: [], invalidTimes: [4000], quoteValid: false,
  });
  assert.equal(screeningCitation(evidence, { frame_timestamps_ms: [], asr_quote: " " }).quoteValid, false);
  assert.equal(screeningCitation(evidence, { frame_timestamps_ms: [], asr_quote: "the container" }).quoteValid, false);
  assert.deepEqual(screeningCitation(undefined, undefined), { validTimes: [], invalidTimes: [], quoteValid: null });
  console.log("Screening contracts passed: isolated paid route, fixed retry key, budget denial, job ownership, cancellation, rejected citations, schema failure, endcards, missing data.");
} finally { globalThis.fetch = originalFetch; }
