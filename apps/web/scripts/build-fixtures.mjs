#!/usr/bin/env node
// Builds the mock-API fixtures in src/api/fixtures from DirectorLoop's recorded outputs in <repo>/data.
// Every file is shaped like a response in docs/API_CREATIVE.md (plus the audit and repair shapes).
// Sanitized: absolute paths become relative /media URLs, answer keys and local paths are dropped,
// and the run fails if anything path-like or secret-like survives.
//
// Usage: npm run fixtures            (from apps/web)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, "..");
const ROOT = path.resolve(WEB, "..", "..");
const DATA = path.join(ROOT, "data");
const OUT = path.join(WEB, "src", "api", "fixtures");

// The two demo videos and the recorded experiment for each. cexp_1a0973ea047_79e85d42 is superseded and ignored.
const VIDEOS = [
  { video_id: "aptip", role: "demo_a", experiment: "cexp_1a097415c48_7bf9dc2b", genome: "genome_97ab80b2a7d30cb3bb97f63b_1.1_gpt-5.6-terra.json" },
  { video_id: "aplaze", role: "demo_b", experiment: "cexp_1a09744b816_2009893a", genome: "genome_bee8fd5a72d36a88cc51e69e_1.1_gpt-5.6-terra.json" },
];

const sources = [];
function rel(p) {
  return path.relative(ROOT, p).split(path.sep).join("/");
}
function readJson(p, { optional = false } = {}) {
  if (!fs.existsSync(p)) {
    if (optional) return null;
    throw new Error(`missing source file: ${rel(p)}`);
  }
  sources.push(rel(p));
  return JSON.parse(fs.readFileSync(p, "utf8"));
}
function fail(msg) {
  console.error(`build-fixtures: ${msg}`);
  process.exit(1);
}
const s1 = (ms) => (ms / 1000).toFixed(1);

// ---------------------------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------------------------

const policyRaw = readJson(path.join(DATA, "creative", "policy.json"));
const refsRaw = readJson(path.join(DATA, "corpus", "curio_references.json"), { optional: true });
const hooksDir = path.join(DATA, "creative", "cache", "hooks");

const loaded = VIDEOS.map((v) => {
  const exp = readJson(path.join(DATA, "creative", "experiments", `${v.experiment}.json`));
  const detail = readJson(path.join(DATA, "creative", "experiments", `${v.experiment}.detail.json`));
  const genome = readJson(path.join(DATA, "creative", "genomes", v.genome));
  if (exp.video_id !== v.video_id) fail(`${v.experiment} is for ${exp.video_id}, expected ${v.video_id}`);
  if (exp.status !== "completed") fail(`${v.experiment} status is ${exp.status}`);
  if (exp.genome_id !== genome.id) fail(`${v.genome} (id ${genome.id}) does not match ${v.experiment} genome_id ${exp.genome_id}`);
  const suite = readJson(path.join(DATA, "creative", "cache", `suite_${genome.artifact_hash.slice(0, 24)}_v1.json`));
  if (suite.id !== exp.suite_id) fail(`suite ${suite.id} does not match ${exp.suite_id}`);
  const ref = refsRaw?.references?.find((r) => r.artifact_hash === genome.artifact_hash) ?? null;
  return { ...v, exp, detail, genome, suite, title: ref?.title ?? v.video_id, source: ref?.source ?? "owned_curio" };
});

// ---------------------------------------------------------------------------------------------
// Shared mappers
// ---------------------------------------------------------------------------------------------

const renderUrl = (hash) => (hash ? `/media/renders/${hash}.mp4` : null);
function hookUrl(hash) {
  if (!hash) return null;
  const file = `${hash.slice(0, 40)}_hook3000.mp4`;
  return fs.existsSync(path.join(hooksDir, file)) ? `/media/hooks/${file}` : null;
}
const isFeature = (v) => v !== null && typeof v === "object" && "value" in v && "source" in v && "confidence" in v;
const feature = (f) => ({ value: f.value, source: f.source, confidence: f.confidence, note: f.note ?? "" });

function hypothesisApi(h) {
  return {
    id: h.id,
    family: h.family,
    statement: h.statement,
    region_start_ms: h.region_start_ms,
    region_end_ms: h.region_end_ms,
    region_basis: h.region_basis,
    detection_confidence: h.detection_confidence,
    evidence: h.evidence.map((e) => ({ kind: e.kind, text: e.text, source: e.source ?? null, ref: e.ref ?? null })),
    counterevidence: h.counterevidence.map((e) => ({ kind: e.kind, text: e.text })),
    candidate_mutations: h.candidate_mutations,
    changed_variable: h.changed_variable,
  };
}

// directorloop/creative/investigate.py: without a retention curve the weak region is the region of the
// hypothesis with the highest detection confidence, described from structure.
function weakRegion(hypotheses) {
  if (!hypotheses.length) return null;
  const top = [...hypotheses].sort((a, b) => b.detection_confidence - a.detection_confidence)[0];
  return {
    start_ms: top.region_start_ms,
    end_ms: top.region_end_ms,
    basis: "creative structure",
    evidence_class: "model_eval",
    description: `potential weak region from structure: ${top.family} at ${s1(top.region_start_ms)}-${s1(top.region_end_ms)}s`,
  };
}

function genomeApi(g) {
  return {
    artifact_hash: g.artifact_hash,
    duration_ms: g.duration_ms,
    beats: g.beats.map((b) => ({
      id: b.id,
      index: b.index,
      start_ms: b.start_ms,
      end_ms: b.end_ms,
      text: b.text,
      role: b.role,
      role_confidence: b.role_confidence,
      is_question: b.is_question,
      is_claim: b.is_claim,
      redundant_with_beat_id: b.redundant_with_beat_id ?? null,
    })),
    shots: g.shots.map((s) => ({ id: s.id, start_ms: s.start_ms, end_ms: s.end_ms })),
    hook: Object.fromEntries(Object.entries(g.hook).filter(([, v]) => isFeature(v)).map(([k, v]) => [k, feature(v)])),
    first_proof_ms: feature(g.first_proof_ms),
    first_payoff_ms: feature(g.first_payoff_ms),
    context_before_proof_ms: feature(g.context_before_proof_ms),
    longest_static_span_ms: feature(g.longest_static_span_ms),
    longest_static_span_start_ms: feature(g.longest_static_span_start_ms),
    shots_per_10s: feature(g.shots_per_10s),
    speech_rate_wps: feature(g.speech_rate_wps),
    open_loops: g.open_loops.map((l) => ({ opened_ms: l.opened_ms, resolved_ms: l.resolved_ms ?? null, description: l.description, confidence: l.confidence })),
    motion_curve: g.motion_curve,
  };
}

// Splits the decision reason into one reason per arm. Formats (directorloop/creative/experiment.py decide()):
//   "<prefix>. <arm>: <outcome> (<reason>). <arm>: ..."   and   "<best> wins: <reason>. <arm>: <outcome> (<reason>)."
function armReasons(decision) {
  const out = {};
  if (!decision) return out;
  const ids = Object.keys(decision.per_arm ?? {});
  const text = decision.reason ?? "";
  const hits = ids
    .map((id) => {
      const wins = text.indexOf(`${id} wins: `);
      const plain = text.indexOf(`${id}: `);
      return wins >= 0 ? { id, at: wins, head: `${id} wins: `, win: true } : plain >= 0 ? { id, at: plain, head: `${id}: `, win: false } : null;
    })
    .filter(Boolean)
    .sort((a, b) => a.at - b.at);
  hits.forEach((h, i) => {
    const end = i + 1 < hits.length ? hits[i + 1].at : text.length;
    let seg = text.slice(h.at + h.head.length, end).trim().replace(/\.$/, "");
    if (!h.win) {
      const open = seg.indexOf("(");
      const close = seg.lastIndexOf(")");
      if (open >= 0 && close > open) seg = seg.slice(open + 1, close);
    }
    out[h.id] = seg;
  });
  return out;
}

function armApi(arm, exp, detail, reasons) {
  const control = arm.label === "control";
  const pad = detail.per_arm_detail?.[arm.label] ?? {};
  const m = arm.mutation;
  return {
    id: arm.id,
    label: arm.label,
    status: arm.status,
    media_url: renderUrl(arm.artifact_hash),
    hook_media_url: hookUrl(arm.artifact_hash),
    duration_ms: arm.duration_ms ?? null,
    render_ms: arm.render_ms ?? null,
    outcome: control ? null : exp.decision?.per_arm?.[arm.id] ?? null,
    outcome_reason: control ? null : reasons[arm.id] ?? null,
    mutation: m
      ? {
          type: m.type,
          description: m.description,
          changed_variable: m.changed_variable,
          protected_variables: m.protected_variables,
          hypothesis_id: m.hypothesis_id,
          target_start_ms: m.target_start_ms,
          target_end_ms: m.target_end_ms,
          expected_benefit: m.expected_benefit,
          risk: m.risk,
        }
      : null,
    fitness: arm.fitness
      ? {
          hard_gates_passed: arm.fitness.hard_gates_passed,
          gate_notes: arm.fitness.gate_notes ?? [],
          components: arm.fitness.components.map((c) => ({
            name: c.name,
            value: c.value,
            unit: c.unit,
            better: c.better,
            evidence: c.evidence,
            detail: c.detail,
            trials: c.trials ?? null,
            valid: c.valid ?? null,
          })),
        }
      : null,
    preference_reasons: { full: pad.full_preference_reasons ?? [], hook: pad.hook_preference_reasons ?? [] },
  };
}

function rankingApi(r, exp, overrides = {}) {
  const hyp = exp.hypotheses.find((h) => h.id === r.hypothesis_id);
  const arm = exp.arms.find((a) => a.mutation && a.mutation.type === r.mutation_type);
  const m = arm?.mutation ?? null;
  return {
    mutation: r.mutation_type,
    hypothesis_id: r.hypothesis_id,
    family: hyp?.family ?? "",
    score: r.score,
    detection_confidence: r.detection_confidence,
    reference_support: r.reference_support ?? null,
    policy_mean: r.policy_mean ?? null,
    policy_wins: r.policy_wins,
    policy_losses: r.policy_losses,
    policy_neutral: r.policy_neutral,
    reason: r.reason,
    description: m?.description ?? "",
    changed_variable: m?.changed_variable ?? hyp?.changed_variable ?? "",
    protected_variables: m?.protected_variables ?? [],
    target_start_ms: m?.target_start_ms ?? hyp?.region_start_ms ?? 0,
    target_end_ms: m?.target_end_ms ?? hyp?.region_end_ms ?? 0,
    selected: Boolean(arm),
    ...overrides,
  };
}

function summaryApi(exp) {
  const winner = exp.decision?.winner_arm_id ? exp.arms.find((a) => a.id === exp.decision.winner_arm_id) : null;
  return {
    id: exp.id,
    video_id: exp.video_id,
    created_at: exp.created_at,
    policy_mode: exp.policy_mode,
    generation: exp.generation,
    outcome: exp.decision?.outcome ?? null,
    winner_label: winner?.label ?? null,
    arms: exp.arms.length,
    status: exp.status,
    policy_version_before: exp.policy_version_before,
    policy_version_after: exp.policy_version_after ?? null,
    total_ms: exp.timings_ms?.total_ms ?? null,
    weave_url: exp.weave_url ?? null,
    recorded: true,
  };
}

// ---------------------------------------------------------------------------------------------
// Endpoint fixtures
// ---------------------------------------------------------------------------------------------

const fixtures = {};

fixtures["videos.json"] = loaded.map((v) => ({
  video_id: v.video_id,
  title: v.title,
  duration_ms: v.genome.duration_ms,
  category: v.genome.category,
  source: v.source,
  media_url: `/media/source/${v.video_id}.mp4`,
  role: v.role,
  has_genome: true,
  retention_class: null,
  latest_experiment_id: v.exp.id,
}));

for (const v of loaded) {
  const summary = fixtures["videos.json"].find((x) => x.video_id === v.video_id);
  const hyps = v.exp.hypotheses.map(hypothesisApi);
  fixtures[`video.${v.video_id}.json`] = {
    ...summary,
    genome: genomeApi(v.genome),
    investigation: { weak_region: weakRegion(hyps), hypotheses: hyps, retention_note: v.exp.notes?.[0] ?? "" },
    retention: null,
  };

  const reasons = armReasons(v.exp.decision);
  fixtures[`experiment.${v.exp.id}.json`] = {
    ...summaryApi(v.exp),
    question: v.exp.question,
    hypotheses: hyps,
    ranking: v.exp.ranking.map((r) => rankingApi(r, v.exp)),
    arms: v.exp.arms.map((a) => armApi(a, v.exp, v.detail, reasons)),
    decision: v.exp.decision
      ? {
          outcome: v.exp.decision.outcome,
          winner_arm_id: v.exp.decision.winner_arm_id ?? null,
          reason: v.exp.decision.reason,
          primary_metric: v.exp.decision.primary_metric,
          per_arm: v.exp.decision.per_arm,
        }
      : null,
    policy_updates: (v.exp.policy_updates ?? []).map((u) => ({
      strategy: u.strategy,
      mutation: u.mutation,
      outcome: u.outcome,
      before: u.before,
      after: u.after,
      policy_version: u.policy_version,
    })),
    reference_patterns: (v.detail.reference_patterns ?? []).map((p) => ({
      mutation: p.mutation,
      pattern: p.pattern,
      description: p.description,
      count: p.count,
      total: p.total,
      top: p.top ?? null,
      bottom: p.bottom ?? null,
      label: p.label,
    })),
    suite: { id: v.exp.suite_id, hash: v.exp.suite_hash, questions: v.suite.questions.map((q) => ({ id: q.id, text: q.text })) },
    timings_ms: v.exp.timings_ms,
    model_calls: v.exp.model_calls,
    notes: v.exp.notes ?? [],
  };
}

fixtures["experiments.json"] = loaded
  .map((v) => summaryApi(v.exp))
  .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

// Design previews. Only recorded rankings are used: the experiment's own ranking (learned mode at its
// policy_version_before) and, where a transfer test saved them, both modes before evaluation.
const noMemoryReason = (reason) => reason.replace(/; experiment evidence .*$/, "; experiment evidence ignored (no-memory mode)");
const transferDir = path.join(DATA, "creative", "transfer");
const transferReports = fs.existsSync(transferDir)
  ? fs
      .readdirSync(transferDir)
      .filter((f) => f.endsWith("_transfer_report.json"))
      .sort()
      .reverse()
  : [];

for (const v of loaded) {
  const learned = v.exp.ranking.map((r) => rankingApi(r, v.exp));
  let none = null;
  let noneVersion = v.exp.policy_version_before;
  const reportFile = transferReports.find((f) => f.includes(`_${v.video_id}_`));
  if (reportFile) {
    const rankings = readJson(path.join(transferDir, reportFile.replace("_transfer_report.json", "_rankings_before_evaluation.json")));
    if (rankings.policy_version === v.exp.policy_version_before && Array.isArray(rankings.rankings?.none)) {
      noneVersion = rankings.policy_version;
      none = rankings.rankings.none.map((row) => {
        const base = v.exp.ranking.find((r) => r.mutation_type === row.mutation);
        if (!base) fail(`transfer ranking ${row.mutation} is not in ${v.exp.id}`);
        return rankingApi(base, v.exp, { score: row.score, reason: row.reason, policy_mean: 0.5, policy_wins: 0, policy_losses: 0, policy_neutral: 0 });
      });
    }
  }
  if (!none) {
    // Before any evidence exists both modes rank identically (design.py); only the reason text differs.
    const anyEvidence = v.exp.ranking.some((r) => r.policy_wins || r.policy_losses || r.policy_neutral);
    if (anyEvidence) fail(`${v.video_id}: no recorded no-memory ranking and the learned ranking already used evidence`);
    none = v.exp.ranking.map((r) => rankingApi(r, v.exp, { reason: noMemoryReason(r.reason) }));
  }
  const skipped = (v.exp.notes ?? []).slice(1).filter((n) => /precondition|renders the same plan/.test(n));
  fixtures[`design.${v.video_id}.learned.json`] = { video_id: v.video_id, mode: "learned", policy_version: v.exp.policy_version_before, rankings: learned, skipped };
  fixtures[`design.${v.video_id}.none.json`] = { video_id: v.video_id, mode: "none", policy_version: noneVersion, rankings: none, skipped };
}

// Policy
fixtures["policy.json"] = {
  version: policyRaw.version,
  strategies: [...policyRaw.strategies]
    .sort((a, b) => b.wins - a.wins || a.losses - b.losses || a.id.localeCompare(b.id))
    .map((s) => ({
      id: s.id,
      mutation: s.mutation_type,
      scope: `${s.scope_level}:${s.scope_value}`,
      status: s.status,
      wins: s.wins,
      losses: s.losses,
      neutral: s.neutral,
      rejected: s.rejected,
      human: s.human_total ? `${s.human_prefer}/${s.human_total}` : null,
      reference_prior: s.reference_prior ?? null,
      confidence: s.confidence,
      evidence: s.evidence.map((e) => ({ experiment_id: e.experiment_id, video_id: e.video_id, outcome: e.outcome, note: e.note, weave_url: e.weave_url ?? null })),
    })),
  changes: policyRaw.changes.map((c) => ({ version: c.version, experiment_id: c.experiment_id, strategy_id: c.strategy_id, before: c.before, after: c.after, created_at: c.created_at })),
};

// Transfer
fixtures["transfers.json"] = transferReports.map((f) => {
  const r = readJson(path.join(transferDir, f));
  const rankings = readJson(path.join(transferDir, f.replace("_transfer_report.json", "_rankings_before_evaluation.json")), { optional: true });
  const mode = (m) => ({
    order: m.order,
    first_mutation: m.first_mutation ?? null,
    first_outcome_on_b: m.first_outcome_on_b ?? null,
    attempts_until_first_win: m.attempts_until_first_win ?? null,
    model_calls_until_then: m.model_calls_until_then,
    eval_and_render_ms_until_then: m.eval_and_render_ms_until_then,
    note: m.note ?? null,
  });
  return {
    video_id: r.video_id,
    policy_version: r.policy_version,
    oracle_experiment: r.oracle_experiment,
    outcomes_on_b: r.outcomes_on_b,
    modes: { none: mode(r.modes.none), learned: mode(r.modes.learned) },
    first_choice_changed: r.first_choice_changed,
    weave_url: r.weave_url ?? null,
    oracle_weave_url: r.oracle_weave_url ?? null,
    created_at: f.split("_")[0],
  };
});

// Corpus: only the analyzed corpus has pattern counts. Absent means the mock answers 404.
const corpusRaw = readJson(path.join(DATA, "corpus", "curio_corpus_with_patterns.json"), { optional: true });
fixtures["corpus.json"] = corpusRaw
  ? {
      id: corpusRaw.id,
      version: corpusRaw.version,
      references: corpusRaw.references.length,
      with_metrics: corpusRaw.references.filter((r) => r.performance).length,
      patterns: (corpusRaw.patterns ?? []).map((p) => ({
        pattern_id: p.pattern_id,
        description: p.description,
        count: p.count,
        total_comparable: p.total_comparable,
        support_ratio: p.support_ratio,
        top_group_count: p.top_group_count ?? null,
        top_group_total: p.top_group_total ?? null,
        bottom_group_count: p.bottom_group_count ?? null,
        bottom_group_total: p.bottom_group_total ?? null,
        performance_metric: p.performance_metric,
        note: p.note,
      })),
      label: "REFERENCE CREATIVE (owned Curio shorts; descriptive counts)",
    }
  : null;

// Human review: counts only (directorloop/review/server.py summarize()); free-text answers never leave data/.
const armLabel = {};
for (const v of loaded) for (const a of v.exp.arms) armLabel[a.id] = a.label;
const reviewsPath = path.join(DATA, "reviews", "reviews.jsonl");
const reviewRows = fs.existsSync(reviewsPath)
  ? (sources.push(rel(reviewsPath)),
    fs
      .readFileSync(reviewsPath, "utf8")
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => JSON.parse(l)))
  : [];
const groups = new Map();
for (const r of reviewRows) {
  const arms = [r.arm_left, r.arm_right].sort();
  const key = `${r.experiment_id}|${r.test_type}|${arms.join(",")}`;
  if (!groups.has(key)) groups.set(key, { experiment_id: r.experiment_id, test_type: r.test_type, arms, rows: [] });
  groups.get(key).rows.push(r);
}
fixtures["reviews-summary.json"] = {
  pairs: [...groups.values()].map((g) => {
    const preferred = {};
    let ties = 0;
    for (const r of g.rows) {
      if (r.chosen_arm_id) preferred[r.chosen_arm_id] = (preferred[r.chosen_arm_id] ?? 0) + 1;
      else ties += 1;
    }
    return {
      experiment_id: g.experiment_id,
      test_type: g.test_type,
      arms: g.arms,
      arm_labels: Object.fromEntries(g.arms.map((a) => [a, armLabel[a] ?? a.split("_").pop()])),
      n: g.rows.length,
      preferred,
      no_preference: ties,
      label: "BLINDED CONTINUE-WATCHING PREFERENCE (human test; a small convenience sample, not retention)",
    };
  }),
  total_responses: reviewRows.length,
};
// The public review link is a live tunnel to this machine; it is never written into fixtures.
fixtures["reviews-qr.json"] = {
  url: null,
  qr_png: null,
  note: "Mock API: no review server is connected. In live mode this shows the public review link and its QR code.",
};

// ---------------------------------------------------------------------------------------------
// Job replays: the stage messages directorloop/creative/experiment.py emits, rebuilt from the recorded
// experiment, at the recorded relative timings.
// ---------------------------------------------------------------------------------------------

for (const v of loaded) {
  const { exp, detail, genome, suite } = v;
  const t = exp.timings_ms;
  const hyps = exp.hypotheses.map(hypothesisApi);
  const weak = weakRegion(hyps);
  const variants = exp.arms.length - 1;
  const trials = exp.arms[0]?.fitness?.components?.find((c) => c.name === "message_comprehension")?.trials ?? 3;
  const leakFile = path.join(DATA, "creative", "cache", `leakage_${exp.suite_hash.slice(0, 24)}_gpt-5.6-terra.json`);
  const leak = readJson(leakFile, { optional: true });
  const weakQs = leak?.weak_question_ids?.length ? `[${leak.weak_question_ids.map((q) => `'${q}'`).join(", ")}]` : "none";
  const at = {};
  at.genome = t.genome_ms;
  at.diagnose = at.genome + t.investigate_ms;
  at.plan = at.diagnose + t.design_ms;
  at.suite = at.plan + t.suite_ms;
  at.rendered = at.suite + t.render_wall_ms;
  at.evaluate = at.rendered + t.arm_media_ms;
  at.decide = at.evaluate + t.evaluate_wall_ms;
  at.done = t.total_ms;
  at.learn = Math.round((at.decide + at.done) / 2);
  const events = [
    { at_ms: 0, stage: "ANALYZING", message: "building the creative genome of the actual video (cached per file hash)", data: null },
    { at_ms: at.genome, stage: "ANALYZING", message: `genome: ${genome.beats.length} beats, ${genome.shots.length} shots, hook ${genome.hook.hook_type.value}`, data: { genome: detail.genome } },
    { at_ms: at.diagnose, stage: "DIAGNOSING", message: weak ? weak.description : "no weak region found", data: { weak_region: weak, hypotheses: detail.hypotheses, retention_note: exp.notes?.[0] ?? "" } },
    {
      at_ms: at.plan,
      stage: "PLANNING",
      message: "ranked experiments: " + exp.ranking.map((r) => `${r.mutation_type} ${r.score.toFixed(3)}`).join(", "),
      data: { ranking: exp.ranking.map((r) => rankingApi(r, exp)), selected: exp.arms.filter((a) => a.mutation).map((a) => a.mutation.type), reference_patterns: detail.reference_patterns ?? [], policy: detail.policy_view ?? [], skipped: [] },
    },
    { at_ms: at.suite, stage: "PLANNING", message: `frozen suite ${exp.suite_hash.slice(0, 12)} (${suite.questions.length} questions); no-media control weak: ${weakQs}`, data: null },
    { at_ms: at.suite, stage: "RENDERING", message: `rendering control + ${variants} variants in parallel`, data: null },
    {
      at_ms: at.rendered,
      stage: "RENDERING",
      message: `rendered ${exp.arms.length} arms in ${t.render_wall_ms} ms`,
      data: { arms: exp.arms.map((a) => ({ label: a.label, media_url: renderUrl(a.artifact_hash), render_ms: a.render_ms })) },
    },
    { at_ms: at.evaluate, stage: "EVALUATING", message: `evaluating ${exp.arms.length} arms with the same frozen suite (${trials} trials, one question per call, pairwise both orders)`, data: null },
    { at_ms: at.decide, stage: "DECIDING", message: `${exp.decision.outcome.toUpperCase()}: ${exp.decision.reason}`, data: { decision: exp.decision, per_arm_detail: detail.per_arm_detail } },
  ];
  if ((exp.policy_updates ?? []).length) {
    events.push({ at_ms: at.learn, stage: "LEARNING", message: "recording every arm (wins, losses, neutral, rejected) in the creative policy", data: null });
  }
  events.push({ at_ms: at.done, stage: "DONE", message: `experiment ${exp.id} finished in ${t.total_ms} ms`, data: { experiment_id: exp.id, weave_url: exp.weave_url, timings_ms: t } });
  fixtures[`replay.${v.video_id}.json`] = { kind: "experiment", video_id: v.video_id, experiment_id: exp.id, total_ms: t.total_ms, events };
}

// ---------------------------------------------------------------------------------------------
// Director runs, audits and repairs (data/runs, data/audit, data/repairs), shaped like directorloop/api/app.py
// serves them: local paths removed, media and frame URLs added.
// ---------------------------------------------------------------------------------------------

// Free text written by models or tools can quote local file paths (for example a source project location). Those are
// reduced to the file name before anything reaches a fixture.
const ABS_PATH_IN_TEXT = /(?:\/Users|\/home|\/private|\/var\/folders)\/[^\s"'`)\]]+/g;
function redactPaths(value) {
  if (typeof value === "string") return value.replace(ABS_PATH_IN_TEXT, (m) => path.basename(m.replace(/[.,;:]+$/, "")));
  if (Array.isArray(value)) return value.map(redactPaths);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redactPaths(v)]));
  return value;
}

const SHA_RE = /^[0-9a-f]{64}$/;
const stemOf = (p) => (p ? path.basename(p).replace(/\.[^.]+$/, "") : "");
const renderUrlFromPath = (p) => (p && SHA_RE.test(stemOf(p)) ? `/media/renders/${stemOf(p)}.mp4` : null);

// Registry titles: demo videos from the API's DEMO_VIDEOS, reference ids derived the way Services.registry() does.
const appSource = fs.readFileSync(path.join(ROOT, "directorloop", "api", "app.py"), "utf8");
sources.push("directorloop/api/app.py");
const demoTitles = {};
for (const m of appSource.matchAll(/\{"video_id": "([a-z0-9_-]+)", "role": "(demo_[ab])", "path": "[^"]*", "title": "([^"]*)"\}/g)) {
  demoTitles[m[1]] = { role: m[2], title: m[3] };
}
const refById = {};
for (const ref of refsRaw?.references ?? []) {
  const vid = ref.id.replace(/^ref_/, "").toLowerCase().replace(/[^a-z0-9_-]/g, "-").slice(0, 80);
  refById[vid] = ref;
}

const runsDir = path.join(DATA, "runs");
const runFiles = fs.existsSync(runsDir) ? fs.readdirSync(runsDir).filter((f) => /^run_.*\.json$/.test(f)).sort().reverse() : [];
const runs = runFiles.map((f) => readJson(path.join(runsDir, f)));

function runSummary(r) {
  return {
    id: r.id,
    video_id: r.config.video_id,
    status: r.status,
    created_at: r.created_at,
    ended_at: r.ended_at ?? null,
    objective: r.config.objective,
    iteration_budget: r.config.iteration_budget,
    iterations: r.iterations.filter((i) => i.finding_id).length,
    decisions: r.iterations.map((i) => i.decision),
    final_version_id: r.final_version_id,
    final_decision: r.final_decision,
    stop_reason: r.stop_reason,
    weave_url: r.weave_url ?? null,
    launched_via: r.launched_via,
    total_ms: r.timings_ms?.total_ms ?? null,
  };
}

fixtures["runs.json"] = redactPaths(runs.map(runSummary));
for (const r of runs) {
  const { video_path: _vp, ...config } = r.config;
  const { original_path: _op, final_path: _fp, iterations, ...rest } = r;
  const original = `/media/source/${r.config.video_id}.mp4`;
  fixtures[`run.${r.id}.json`] = redactPaths({
    ...rest,
    config,
    iterations: iterations.map(({ candidate_path, ...it }) => ({ ...it, candidate_media_url: renderUrlFromPath(candidate_path) })),
    original_media_url: original,
    final_media_url: renderUrlFromPath(r.final_path) ?? original,
  });
}

const auditIds = new Set(runs.flatMap((r) => [...r.audit_ids, ...r.iterations.flatMap((i) => [i.audit_id, i.candidate_audit_id])]).filter(Boolean));
for (const id of auditIds) {
  const a = readJson(path.join(DATA, "audit", id, "audit.json"), { optional: true });
  if (!a) continue;
  const frameUrl = (p) => (path.basename(path.dirname(p)) === "frames" && /^\d+$/.test(stemOf(p)) ? `/media/audit/${a.id}/${stemOf(p)}.jpg` : null);
  const withUrls = (items) => items.map((it) => ({ ...it, evidence_frames: (it.evidence_frames ?? []).map(({ path: p, ...ef }) => ({ ...ef, url: frameUrl(p) })) }));
  const { artifact_path, ...rest } = a;
  fixtures[`audit.${a.id}.json`] = redactPaths({ ...rest, findings: withUrls(a.findings), strengths: withUrls(a.strengths), media_url: renderUrlFromPath(artifact_path) });
}

const repairIds = new Set(runs.flatMap((r) => [...(r.repair_run_ids ?? []), ...r.iterations.map((i) => i.repair_run_id)]).filter(Boolean));
for (const id of repairIds) {
  const rr = readJson(path.join(DATA, "repairs", `${id}.json`), { optional: true });
  if (!rr) continue;
  const { original_path, candidate_path, ...rest } = rr;
  fixtures[`repair.${rr.id}.json`] = redactPaths({ ...rest, candidate_media_url: renderUrlFromPath(candidate_path) });
}

// A/B-to-C records (data/abc), shaped like GET /api/abc and GET /api/abc/{id}.
const abcDir = path.join(DATA, "abc");
const abcRuns = fs.existsSync(abcDir)
  ? fs
      .readdirSync(abcDir)
      .filter((f) => /^abc_.*\.json$/.test(f))
      .sort()
      .reverse()
      .map((f) => readJson(path.join(abcDir, f)))
  : [];
fixtures["abc.json"] = redactPaths(
  abcRuns.map((r) => ({
    id: r.id,
    status: r.status,
    created_at: r.created_at,
    ended_at: r.ended_at ?? null,
    objective: r.context.objective,
    a_video_id: r.versions.A?.video_id ?? null,
    b_video_id: r.versions.B?.video_id ?? null,
    ab_overall: r.comparison?.whole?.overall?.verdict ?? null,
    attempts: r.attempts.filter((a) => a.proposal).length,
    decisions: r.attempts.map((a) => a.decision),
    final_version: r.final_version,
    final_decision: r.final_decision,
    stop_reason: r.stop_reason,
    weave_url: r.weave_url ?? null,
    launched_via: r.launched_via,
    total_ms: r.timings_ms?.total_ms ?? null,
    rubric_version: r.rubric?.version ?? null,
  })),
);
for (const r of abcRuns) {
  const versions = Object.fromEntries(
    Object.entries(r.versions).map(([k, v]) => {
      const { original_path, evaluated_path, ...rest } = v;
      return [k, { ...rest, media_url: renderUrlFromPath(evaluated_path) }];
    }),
  );
  const attempts = r.attempts.map(({ render_path, ...a }) => ({ ...a, render_media_url: renderUrlFromPath(render_path) }));
  const { state: _state, ...runtime } = r.runtime ?? {};
  fixtures[`abc.${r.id}.json`] = redactPaths({ ...r, versions, attempts, runtime });
  for (const v of Object.values(r.versions)) if (v.audit_id) auditIds.add(v.audit_id);
  for (const a of r.attempts) if (a.evaluation?.candidate_audit_id) auditIds.add(a.evaluation.candidate_audit_id);
}

// Every stored audit, for GET /api/audits?video_id= and GET /api/audits/{id}.
const auditRoot = path.join(DATA, "audit");
const allAuditDirs = fs.existsSync(auditRoot) ? fs.readdirSync(auditRoot).filter((d) => /^audit_/.test(d)).sort().reverse() : [];
const auditSummaries = [];
for (const id of allAuditDirs) {
  const a = readJson(path.join(auditRoot, id, "audit.json"), { optional: true });
  if (!a) continue;
  auditIds.add(a.id);
  auditSummaries.push({ id: a.id, video_id: a.video_id, version_id: a.version_id, status: a.status, created_at: a.created_at, findings: a.findings.length, strengths: a.strengths.length, duration_ms: a.duration_ms, weave_url: a.weave_url ?? null });
}
fixtures["audits.json"] = auditSummaries;
for (const id of auditIds) {
  if (fixtures[`audit.${id}.json`]) continue;
  const a = readJson(path.join(auditRoot, id, "audit.json"), { optional: true });
  if (!a) continue;
  const frameUrl = (p) => (path.basename(path.dirname(p)) === "frames" && /^\d+$/.test(stemOf(p)) ? `/media/audit/${a.id}/${stemOf(p)}.jpg` : null);
  const withUrls = (items) => items.map((it) => ({ ...it, evidence_frames: (it.evidence_frames ?? []).map(({ path: p, ...ef }) => ({ ...ef, url: frameUrl(p) })) }));
  const { artifact_path, ...rest } = a;
  fixtures[`audit.${a.id}.json`] = redactPaths({ ...rest, findings: withUrls(a.findings), strengths: withUrls(a.strengths), media_url: renderUrlFromPath(artifact_path) });
}

// Videos: the demo videos plus every video a stored run used, in the /api/videos shape.
const videoIds = [...new Set([...Object.keys(demoTitles), ...runs.map((r) => r.config.video_id)])];
fixtures["videos.json"] = videoIds.map((vid) => {
  const demo = demoTitles[vid];
  const ref = refById[vid];
  const latest = loaded.find((v) => v.video_id === vid)?.exp.id ?? null;
  return {
    video_id: vid,
    title: demo?.title ?? ref?.title ?? vid,
    duration_ms: null,
    category: "educational_short",
    source: demo ? "owned_curio" : ref?.source ?? "owned_curio",
    media_url: `/media/source/${vid}.mp4`,
    role: demo?.role ?? "reference",
    has_genome: true,
    retention_class: null,
    latest_experiment_id: latest,
    edit_permission: "owned",
    platform: null,
  };
});

for (const v of fixtures["videos.json"]) {
  const detail = fixtures[`video.${v.video_id}.json`];
  if (detail) Object.assign(detail, { title: v.title, role: v.role, source: v.source });
}

// Mock health: nothing can run without the API, so every feature is reported off.
fixtures["health.json"] = {
  status: "ok",
  features: { runs: false, abc: false, url_ingest: false, classify: false },
  weave: { connected: false, project: "leondragon3798-curio/directorloop", traces_url: null, reason: "mock API: not connected" },
  providers: [],
  policy_version: policyRaw.version,
  corpus: corpusRaw ? { references: corpusRaw.references.length, with_metrics: corpusRaw.references.filter((r) => r.performance).length, patterns: (corpusRaw.patterns ?? []).length } : { references: 0, with_metrics: 0, patterns: 0 },
  review: { public_url: null, local_url: "http://127.0.0.1:8790" },
};

// ---------------------------------------------------------------------------------------------
// Sanitize check and write
// ---------------------------------------------------------------------------------------------

const FORBIDDEN = [
  [/\/Users\//, "absolute macOS path"],
  [/\/home\//, "absolute home path"],
  [/\/private\//, "absolute private path"],
  [/[A-Za-z]:\\\\/, "absolute Windows path"],
  [/file:\/\//, "file URL"],
  [/correct_option_id/, "answer key"],
  [/artifact_path|media_path|production_dir/, "local path field"],
  [/sk-[A-Za-z0-9_-]{16,}/, "API key"],
  [/(api[_-]?key|secret|password|bearer)\s*["':=]/i, "secret-like field"],
  [/trycloudflare\.com/, "live tunnel URL"],
];
fixtures["manifest.json"] = {
  built_at: new Date().toISOString(),
  generator: "apps/web/scripts/build-fixtures.mjs",
  sources: [...new Set(sources)].sort(),
  notes: [
    "Experiments, genomes, policy, transfer and review counts are converted from recorded outputs.",
    "Runs, audits and repairs are the stored director records in the shapes the API serves, with local paths removed.",
    "Mock health reports every feature off: the mock API cannot upload, judge or improve.",
  ],
};

let bad = 0;
for (const [name, value] of Object.entries(fixtures)) {
  const text = JSON.stringify(value);
  for (const [re, what] of FORBIDDEN) {
    const m = text.match(re);
    if (m) {
      bad += 1;
      console.error(`build-fixtures: ${name} contains a ${what} near "${text.slice(Math.max(0, m.index - 40), m.index + 40)}"`);
    }
  }
}
if (bad) fail(`${bad} sanitization problem(s); nothing written`);

fs.mkdirSync(OUT, { recursive: true });
for (const f of fs.readdirSync(OUT)) if (f.endsWith(".json")) fs.unlinkSync(path.join(OUT, f));
for (const [name, value] of Object.entries(fixtures)) {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(value, null, 2) + "\n");
}
const size = Object.keys(fixtures).reduce((n, k) => n + fs.statSync(path.join(OUT, k)).size, 0);
console.log(`build-fixtures: wrote ${Object.keys(fixtures).length} files (${(size / 1024).toFixed(0)} KB) to ${rel(OUT)}`);
console.log(`  sources: ${[...new Set(sources)].length} files; corpus ${corpusRaw ? "built" : "not built (mock answers 404)"}; human responses ${reviewRows.length}`);
