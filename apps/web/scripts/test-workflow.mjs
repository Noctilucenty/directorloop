// Pure data-contract checks; no browser, API, or model calls.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";

const source = await fs.readFile(new URL("../src/lib/workflow.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const { buildWorkflow, iterationContinuation, workflowTraceUrl } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
const fixture = async (name) => JSON.parse(await fs.readFile(new URL(`../src/api/fixtures/${name}`, import.meta.url), "utf8"));
const step = (id, extra = {}) => ({ id, parent_id: null, key: "render", title: "Render candidate", kind: "tool", iteration: 1, status: "running", started_at: "2026-09-12T21:00:00Z", ended_at: null, duration_ms: null, weave_call_id: null, weave_url: null, inputs: {}, summary: {}, ...extra });
let checked = 0;
function check(name, callback) { callback(); checked += 1; process.stdout.write(`ok ${checked} - ${name}\n`); }

check("empty state invents no steps", () => assert.deepEqual(buildWorkflow(), { nodes: [], stages: [], source: "empty" }));
check("event replay preserves a completed persisted span and its measured timing", () => {
  const done = step("render", { status: "completed", ended_at: "2026-09-12T21:00:02Z", duration_ms: 2000 });
  const result = buildWorkflow([done], [{ seq: 1, stage: "WORKFLOW_STAGE", data: { stage: step("render") } }]);
  assert.equal(result.stages[0].status, "completed");
  assert.equal(result.stages[0].duration_ms, 2000);
});
check("parent IDs define hierarchy even if children arrive first", () => {
  const result = buildWorkflow([step("child", { parent_id: "parent" }), step("parent", { key: "iteration" })]);
  assert.equal(result.nodes.length, 1);
  assert.equal(result.nodes[0].id, "parent");
  assert.equal(result.nodes[0].children[0].id, "child");
});
check("concurrent review windows display in video-time order, preserving their span timestamps", () => {
  const result = buildWorkflow([
    step("review", { key: "cold_judge" }),
    step("later", { key: "review_window", parent_id: "review", inputs: { start_ms: 4000 }, started_at: "2026-09-12T21:00:00Z" }),
    step("earlier", { key: "review_window", parent_id: "review", inputs: { start_ms: 0 }, started_at: "2026-09-12T21:00:01Z" }),
  ]);
  assert.deepEqual(result.nodes[0].children.map((node) => node.id), ["earlier", "later"]);
  assert.equal(result.nodes[0].children[0].started_at, "2026-09-12T21:00:01Z");
});
check("orphans and cycles remain inspectable without recursive loops", () => {
  const result = buildWorkflow([step("a", { parent_id: "b" }), step("b", { parent_id: "a" }), step("orphan", { parent_id: "missing" })]);
  assert.equal(result.nodes.length, 3);
  assert.ok(result.nodes.every((n) => n.children.length === 0));
});
check("broad job phases do not become fake live spans", () => {
  const result = buildWorkflow([], [{ seq: 1, stage: "RENDERING", data: null }]);
  assert.equal(result.source, "empty");
});

const abc = await fixture("abc.abc_1a09788a1d4_303d76b2.json");
check("recorded A/B attempt does not invent C for a declined proposal or split combined timing", () => {
  const result = buildWorkflow([], [], abc);
  assert.equal(result.source, "record");
  assert.equal(result.stages.filter((s) => s.key === "render").length, 1);
  assert.equal(result.stages.find((s) => s.key === "render").duration_ms, null);
  assert.ok(result.stages.every((s) => s.status !== "running"));
  assert.ok(result.stages.filter((s) => s.parent_id !== null).every((s) => s.weave_url === null));
  const iteration = result.stages.find((s) => s.key === "iteration" && s.iteration === 1);
  assert.equal(iterationContinuation(iteration).accepted, false);
  assert.match(iterationContinuation(iteration).title, /Keep the baseline/);
});
check("a failed verification with an evaluation shell does not fabricate a comparison", () => {
  const failed = structuredClone(abc);
  failed.attempts = [failed.attempts[0]];
  failed.attempts[0].evaluation = {
    change_verification: { verified: false, intended: "test", checks: ["The edit did not happen"] },
    candidate_audit_id: null, vs_base: null, vs_other: null, target_region: null,
    outcome: "insufficient_evidence", outcome_reason: "Not compared", improved: [], regressed: [], uncertain: [], new_weaknesses: [],
  };
  const result = buildWorkflow([], [], failed);
  assert.equal(result.stages.filter((node) => node.key === "compare" && node.iteration === 1).length, 0);
  assert.equal(result.stages.filter((node) => node.key === "verify").length, 1);
});
const single = await fixture("run.run_1a097719df3_7d704e04.json");
check("recorded single-video rejection retains evidence and measured render duration", () => {
  const result = buildWorkflow([], [], single);
  const render = result.stages.find((s) => s.key === "render");
  assert.equal(render.duration_ms, single.iterations[0].timings_ms.render_ms);
  assert.equal(result.stages.find((s) => s.key === "verify").summary.verified, true);
  assert.equal(result.stages.find((s) => s.key === "decision").summary.decision, "reject_keep_current");
  assert.equal(result.stages.find((s) => s.key === "final_selection").summary.retained_version, "v0");
  assert.deepEqual(result.nodes[0].summary.annotations, single.annotations);
});
check("real stages replace record reconstruction instead of double counting", () => {
  const result = buildWorkflow([step("real")], [], abc);
  assert.equal(result.source, "stage");
  assert.equal(result.stages.length, 1);
});
check("unsafe evidence links are not rendered", () => {
  assert.equal(workflowTraceUrl("javascript:alert(1)"), null);
  assert.equal(workflowTraceUrl("https://wandb.ai.attacker.example/run"), null);
  assert.equal(workflowTraceUrl("https://user:secret@wandb.ai/run"), null);
  assert.equal(workflowTraceUrl("https://wandb.ai/team/project/weave/calls/real"), "https://wandb.ai/team/project/weave/calls/real");
});
process.stdout.write(`${checked} workflow contract checks passed.\n`);
