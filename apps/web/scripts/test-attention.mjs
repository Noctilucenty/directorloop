// Checks how the judge view reads per-window attention predictions. Pure logic; no browser, API or model calls.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";

const source = await fs.readFile(new URL("../src/lib/attention.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
const { readAttention, reviewed } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

/** Windows in the API's shape, 2 s each unless an end is given. */
const windows = (spec) =>
  spec.map((s, i) => {
    const [risk, reaction = "engaged", end] = s.split("/");
    return {
      start_ms: i * 2000,
      end_ms: end ? Number(end) : (i + 1) * 2000,
      understanding: "",
      expectation: "",
      open_question: "",
      reaction,
      attention_risk: risk,
      cause: "",
      label: "MODEL JUDGMENT",
      latency_ms: 0,
      error: risk === "unknown" ? "provider error" : null,
    };
  });

let checked = 0;
function check(name, fn) {
  fn();
  checked += 1;
  process.stdout.write(`ok ${checked} - ${name}\n`);
}

check("no windows gives no reading", () => {
  const s = readAttention([]);
  assert.equal(s.reviewedCount, 0);
  assert.equal(s.maxRisk, null);
});

check("all low has no rise, no peak run above low and no recovery", () => {
  const s = readAttention(windows(["low", "low", "low"]));
  assert.equal(s.maxRisk, "low");
  assert.equal(s.firstRise, null);
  assert.equal(s.recovery, null);
  assert.equal(s.endsElevated, false);
});

check("low, low, medium, high, high, low: first rise, peak stretch and recovery", () => {
  const s = readAttention(windows(["low", "low", "medium/neutral", "high/losing_interest", "high/confused", "low/engaged/11100"]));
  assert.equal(s.firstRise, 2);
  assert.deepEqual(s.firstStretch, [2, 4]);
  assert.equal(s.maxRisk, "high");
  assert.deepEqual(s.peakRuns, [[3, 4]]);
  assert.equal(s.recovery, 5);
  assert.equal(s.endsElevated, false);
});

check("a video that starts at high risk needs no earlier low window", () => {
  const s = readAttention(windows(["high", "high", "high"]));
  assert.equal(s.firstRise, 0);
  assert.deepEqual(s.peakRuns, [[0, 2]]);
  assert.equal(s.recovery, null);
  assert.equal(s.endsElevated, true);
});

check("separate high stretches stay separate", () => {
  const s = readAttention(windows(["low", "high", "low", "high"]));
  assert.deepEqual(s.peakRuns, [[1, 1], [3, 3]]);
  assert.equal(s.recovery, 2);
  assert.equal(s.endsElevated, true);
});

check("a window without a reading breaks a run and is not counted as low", () => {
  const w = windows(["low", "high", "unknown", "high", "low"]);
  const s = readAttention(w);
  assert.equal(reviewed(w[2]), false);
  assert.equal(s.reviewedCount, 4);
  assert.deepEqual(s.peakRuns, [[1, 1], [3, 3]]);
  assert.deepEqual(s.firstStretch, [1, 1]);
  assert.equal(s.recovery, 4);
});

check("windows are read in time order whatever order the API lists them", () => {
  const w = windows(["low", "medium", "low"]).reverse();
  const s = readAttention(w);
  assert.equal(s.windows[0].start_ms, 0);
  assert.equal(s.firstRise, 1);
  assert.equal(s.recovery, 2);
});

check("the last partial window keeps its real end time", () => {
  const s = readAttention(windows(["low", "low", "low", "low", "low", "medium/neutral/11100"]));
  assert.equal(s.windows[5].end_ms, 11100);
  assert.equal(s.endsElevated, true);
  assert.equal(s.recovery, null);
});

process.stdout.write(`${checked} attention reading checks passed.\n`);
