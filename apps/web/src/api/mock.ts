// In-browser simulated API for development without a backend. Every payload here is
// invented mock data and is labeled as such in the UI (environment "mock").
import type { ApiClient, ReviewAnswers } from "./client";
import { ApiError } from "./client";
import type {
  AssetView,
  BenchmarkRun,
  Comparison,
  CreativeBrief,
  EditPlan,
  EvaluationRun,
  EvaluationSummary,
  FailureFinding,
  HealthReady,
  JobEvent,
  JobView,
  PolicyStore,
  ProjectDetail,
  ProjectSummary,
  PromotionDecision,
  ProviderEntry,
  QuestionView,
  RepairProposal,
  ReviewAssignment,
  ReviewSummary,
  VersionDetail,
  VersionView,
} from "./types";
import { isTerminal } from "./types";

const PROJECT_ID = "proj_pack_a";
const SUITE_HASH = "9c1f2a7e4b3d5c6a7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6";

const QUESTIONS: QuestionView[] = [
  { id: "q_material", text: "What is the stand made of?", modality: "either", regression_guard: true, leakage_status: "strong" },
  { id: "q_object", text: "What kind of object is being demonstrated?", modality: "either", regression_guard: true, leakage_status: "strong" },
  { id: "q_tap_stable", text: "What happens to the phone when the screen is tapped after locking?", modality: "visual", regression_guard: true, leakage_status: "strong" },
  { id: "q_lock_action", text: "What physical action locks the stand?", modality: "visual", regression_guard: false, leakage_status: "strong" },
  { id: "q_tab_location", text: "Where does the orange tab end up after the stand is locked?", modality: "visual", regression_guard: false, leakage_status: "strong" },
  { id: "q_moving_part", text: "Which part of the stand moves during locking?", modality: "visual", regression_guard: false, leakage_status: "strong" },
];

const BRIEF: CreativeBrief = {
  id: "brief_pack_a",
  project_id: PROJECT_ID,
  profile: "product_demo",
  objective: "Make it immediately clear how the stand locks and that the phone stays put",
  audience: "general",
  language: "en",
  aspect_ratio: "9:16",
  target_duration_ms_min: 8000,
  target_duration_ms_max: 12000,
  required_information: [
    { id: "ri_lock", text: "How the stand locks", claim_ids: ["c_lock_action", "c_tab_location", "c_moving_part"] },
    { id: "ri_stable", text: "The phone stays put when tapped", claim_ids: ["c_result_stable"] },
  ],
  style_notes: "Creator first-draft voiceover; plain captions; keep it brisk.",
  protected_constraints: [
    { id: "pc_narr", kind: "keep_narration", reason: "Narration is the creator's voice", start_ms: null, end_ms: null, value_ms: null },
    { id: "pc_music", kind: "keep_music", reason: "Original bed", start_ms: null, end_ms: null, value_ms: null },
    { id: "pc_look", kind: "preserve_product_appearance", reason: "Do not change the stand", start_ms: null, end_ms: null, value_ms: null },
    { id: "pc_facts", kind: "no_new_facts", reason: "Only claims in source truth", start_ms: null, end_ms: null, value_ms: null },
    { id: "pc_max", kind: "max_duration_ms", reason: "Short form", start_ms: null, end_ms: null, value_ms: 12000 },
  ],
  allowed_actions: [
    "trim_or_retime",
    "reorder_segments",
    "replace_with_existing_asset",
    "crop_existing_shot",
    "revise_captions",
    "generate_missing_shot",
  ],
  generation_permitted: true,
  narration_change_permitted: false,
  budget: { max_usd: 2, max_llm_calls: 40, max_generation_calls: 1, max_wall_seconds: 45 },
  review_status: "approved",
  approved_by: "leon",
};

const ASSETS: AssetView[] = [
  { id: "asset_wide", label: "wide shot", kind: "video", origin: "rendered_fixture", duration_ms: 6000, width: 1080, height: 1920, media_url: "/mock/v0.mp4", content_hash: "a1".repeat(32) },
  { id: "asset_closeup", label: "close-up", kind: "video", origin: "rendered_fixture", duration_ms: 3000, width: 1080, height: 1920, media_url: "/mock/v1.mp4", content_hash: "b2".repeat(32) },
  { id: "asset_result", label: "result shot", kind: "video", origin: "rendered_fixture", duration_ms: 4000, width: 1080, height: 1920, media_url: "/mock/v0.mp4", content_hash: "c3".repeat(32) },
  { id: "asset_narration", label: "narration", kind: "audio", origin: "rendered_fixture", duration_ms: 7800, width: null, height: null, media_url: null, content_hash: "d4".repeat(32) },
  { id: "asset_music", label: "music bed", kind: "audio", origin: "rendered_fixture", duration_ms: 12000, width: null, height: null, media_url: null, content_hash: "e5".repeat(32) },
];

const BASE_PLAN: EditPlan = {
  schema_version: "1",
  parent_plan_hash: null,
  output: { width: 720, height: 1280, fps_num: 30, fps_den: 1 },
  segments: [
    { id: "seg_wide", asset_id: "asset_wide", source_in_ms: 0, source_out_ms: 5500, fit: "cover", crop: null, speed: 1, audio_policy: "mute", label: "wide" },
    { id: "seg_result", asset_id: "asset_result", source_in_ms: 0, source_out_ms: 4000, fit: "cover", crop: null, speed: 1, audio_policy: "mute", label: "result" },
  ],
  captions: [
    { id: "cap_01", text: "Cereal-box phone stand", start_ms: 300, end_ms: 2500, position: "bottom", style: "default" },
    { id: "cap_02", text: "Locks in one move", start_ms: 3000, end_ms: 5000, position: "bottom", style: "default" },
    { id: "cap_03", text: "Holds when you tap", start_ms: 6500, end_ms: 9200, position: "bottom", style: "default" },
  ],
  narration: { asset_id: "asset_narration", offset_ms: 0, gain_db: 0 },
  music: { asset_id: "asset_music", gain_db: -16 },
  protected_intervals: [],
  change_rationale: "Creator first cut: establishing wide shot, then the result.",
};

function questionSummaries(passed: Record<string, boolean>) {
  return QUESTIONS.map((q) => {
    const ok = passed[q.id];
    return {
      question_id: q.id,
      modality: q.modality,
      weight: 1,
      regression_guard: q.regression_guard,
      valid_trials: 3,
      errors: 0,
      correct: ok ? 3 : 0,
      pass_rate: ok ? 1 : 0,
      passed: ok,
      chosen: (ok ? { correct: 3 } : { not_shown: 3 }) as Record<string, number>,
    };
  });
}

function probeResults(passed: Record<string, boolean>) {
  const out = [] as EvaluationRun["results"];
  for (const q of QUESTIONS) {
    for (let t = 0; t < 3; t += 1) {
      out.push({
        question_id: q.id,
        trial: t,
        chosen_option_id: passed[q.id] ? `opt_${q.id}_a` : "not_shown",
        correct: passed[q.id],
        error: null,
        latency_ms: 1800 + t * 90,
      });
    }
  }
  return out;
}

function evalRun(
  id: string,
  versionId: string,
  hash: string,
  passed: Record<string, boolean>,
  mode: EvaluationRun["mode"],
  mechanical: EvaluationRun["mechanical"],
): EvaluationRun {
  const passedCount = Object.values(passed).filter(Boolean).length;
  const mechanicalPassed = mechanical.every((m) => m.passed || m.severity !== "critical");
  return {
    id,
    version_id: versionId,
    artifact_hash: hash,
    suite_id: "suite_stand_demo",
    suite_hash: SUITE_HASH,
    mode,
    probe_modality: "video_native",
    provider: "gemini",
    model: "gemini-3.8-flash",
    trials: 3,
    frames_sampled: null,
    frame_timestamps_ms: [],
    transcript_source: "whisper.cpp",
    transcript_text: "I made this phone stand out of a cereal box. Set it up, lock it, and it holds. Even when you tap around on the screen.",
    results: probeResults(passed),
    question_summaries: questionSummaries(passed),
    mechanical,
    constraints: [
      { constraint_id: "pc_narr", kind: "keep_narration", passed: true, detail: "narration asset unchanged" },
      { constraint_id: "pc_music", kind: "keep_music", passed: true, detail: "music asset unchanged" },
      { constraint_id: "pc_look", kind: "preserve_product_appearance", passed: true, detail: "no generated assets" },
      { constraint_id: "pc_facts", kind: "no_new_facts", passed: true, detail: "caption text unchanged" },
      { constraint_id: "pc_max", kind: "max_duration_ms", passed: true, detail: "within 12000 ms" },
    ],
    questions_passed: passedCount,
    questions_total: QUESTIONS.length,
    trials_correct: passedCount * 3,
    trials_valid: QUESTIONS.length * 3,
    trials_errored: 0,
    score: passedCount / QUESTIONS.length,
    mechanical_passed: mechanicalPassed,
    constraints_passed: true,
    started_at: "2026-09-12T18:02:11.000Z",
    ended_at: "2026-09-12T18:02:17.400Z",
    latency_ms: 6400,
    probe_latency_ms: 5900,
    cost_usd_estimate: 0.012,
    weave_call_id: null,
    weave_url: null,
    cache_key: `${hash.slice(0, 12)}:${SUITE_HASH.slice(0, 12)}:gemini-3.8-flash:v1`,
    notes: ["mock data: not a measurement"],
  };
}

const MECH_OK: EvaluationRun["mechanical"] = [
  { id: "decodes", passed: true, severity: "critical", value: "ok", threshold: null, detail: "ffprobe decoded all streams", measurement_type: "mechanical" },
  { id: "duration_in_bounds", passed: true, severity: "critical", value: 9500, threshold: "8000-12000", detail: "9500 ms", measurement_type: "mechanical" },
  { id: "audio_present", passed: true, severity: "critical", value: "aac", threshold: null, detail: "audio stream present", measurement_type: "mechanical" },
  { id: "captions_fit", passed: true, severity: "critical", value: 3, threshold: null, detail: "3 captions inside safe area", measurement_type: "mechanical" },
  { id: "narration_within_timeline", passed: true, severity: "critical", value: 7800, threshold: 9500, detail: "narration ends before video end", measurement_type: "mechanical" },
  { id: "assets_authorized", passed: true, severity: "critical", value: "ok", threshold: null, detail: "all segment assets in manifest", measurement_type: "mechanical" },
];

const MECH_TRIM_FAIL: EvaluationRun["mechanical"] = MECH_OK.map((m) =>
  m.id === "narration_within_timeline"
    ? { ...m, passed: false, value: 7800, threshold: 6200, detail: "narration (7800 ms) runs 1600 ms past the video end (6200 ms)" }
    : m.id === "duration_in_bounds"
      ? { ...m, passed: false, value: 6200, threshold: "8000-12000", detail: "6200 ms is below the 8000 ms minimum" }
      : m,
);

const BASE_PASSED = { q_material: true, q_object: true, q_tap_stable: true, q_lock_action: false, q_tab_location: false, q_moving_part: false };
const TRIM_PASSED = { q_material: true, q_object: true, q_tap_stable: false, q_lock_action: false, q_tab_location: false, q_moving_part: false };
const FIXED_PASSED = { q_material: true, q_object: true, q_tap_stable: true, q_lock_action: true, q_tab_location: true, q_moving_part: true };

const V0_HASH = "f0e1d2c3b4a5968778695a4b3c2d1e0f".repeat(2);
const V1_HASH = "0badc0ffee1234567890abcdef123456".repeat(2);
const V2_HASH = "5eed5eed5eed5eed5eed5eed5eed5eed".repeat(2);

const V0_EVAL = evalRun("eval_v0", "ver_0", V0_HASH, BASE_PASSED, "fresh", MECH_OK);
const V1_EVAL = evalRun("eval_v1", "ver_1", V1_HASH, TRIM_PASSED, "fresh", MECH_TRIM_FAIL);

function summary(run: EvaluationRun): EvaluationSummary {
  return {
    run_id: run.id,
    mode: run.mode,
    probe_modality: run.probe_modality,
    provider: run.provider,
    model: run.model,
    trials: run.trials,
    questions_passed: run.questions_passed,
    questions_total: run.questions_total,
    score: run.score,
    mechanical_passed: run.mechanical_passed,
    constraints_passed: run.constraints_passed,
    failed_question_ids: run.question_summaries.filter((q) => !q.passed).map((q) => q.question_id),
    latency_ms: run.latency_ms,
    weave_url: run.weave_url,
  };
}

const TRIM_PLAN: EditPlan = {
  ...BASE_PLAN,
  parent_plan_hash: "plan0".padEnd(64, "0"),
  segments: [
    { ...BASE_PLAN.segments[0], source_in_ms: 1800, source_out_ms: 4000 },
    BASE_PLAN.segments[1],
  ],
  change_rationale: "Trimmed the wide shot to focus on the locking moment.",
};

const FIXED_PLAN: EditPlan = {
  ...BASE_PLAN,
  parent_plan_hash: "plan0".padEnd(64, "0"),
  segments: [
    { ...BASE_PLAN.segments[0], source_out_ms: 2000 },
    { id: "seg_closeup", asset_id: "asset_closeup", source_in_ms: 0, source_out_ms: 2500, fit: "cover", crop: null, speed: 1, audio_policy: "mute", label: "close-up" },
    { id: "seg_wide_tail", asset_id: "asset_wide", source_in_ms: 3500, source_out_ms: 5500, fit: "cover", crop: null, speed: 1, audio_policy: "mute", label: "wide tail" },
    BASE_PLAN.segments[1],
  ],
  change_rationale: "Show the existing close-up at the moment the tab locks; narration and captions unchanged.",
};

const FINDING: FailureFinding = {
  id: "find_0001",
  category: "MISSING_ACTION_VISIBILITY",
  severity: 0.9,
  confidence: 0.78,
  start_ms: 2000,
  end_ms: 3500,
  timestamp_precision: "segment",
  observed: "9 of 9 probe trials on the three mechanism questions answered 'not shown'. Material and object questions passed 3/3.",
  inferred_cause: "The locking action happens while the tab is under 3 percent of frame height in the wide shot; the narration mentions locking but never describes the mechanism.",
  alternatives: ["Model recognition error on a small object", "Action happens too briefly to sample"],
  evidence: {
    failed_question_ids: ["q_lock_action", "q_tab_location", "q_moving_part"],
    probe_failures: 9,
    probe_trials: 18,
    mechanical_check_ids: [],
    constraint_ids: [],
    claim_ids: ["c_lock_action", "c_tab_location", "c_moving_part"],
    transcript_mentions_claim: false,
    coverage_note: "close-up asset verified to show the tab entering the slot (clear); wide shot verified but small",
  },
  measurement_type: "model_probe",
  editable_dimensions: ["replace_with_existing_asset", "crop_existing_shot", "generate_missing_shot"],
  affected_segment_ids: ["seg_wide"],
};

const PROPOSAL_FIXED: RepairProposal = {
  id: "prop_0002",
  finding_id: "find_0001",
  hypothesis: "Viewers cannot see the tab move in the wide shot. Showing the existing close-up during the locking moment should make the mechanism questions answerable without changing narration.",
  evidence_refs: ["eval_v0", "coverage:asset_closeup:c_lock_action"],
  action: "replace_with_existing_asset",
  ops: [
    { type: "trim_segment", segment_id: "seg_wide", source_in_ms: 0, source_out_ms: 2000 },
    { type: "insert_segment", after_segment_id: "seg_wide", segment: { id: "seg_closeup", asset_id: "asset_closeup", source_in_ms: 0, source_out_ms: 2500 } },
    { type: "insert_segment", after_segment_id: "seg_closeup", segment: { id: "seg_wide_tail", asset_id: "asset_wide", source_in_ms: 3500, source_out_ms: 5500 } },
  ],
  predicted_benefit: "Prediction: the three mechanism questions flip to pass; guards unaffected.",
  expected_latency: { expected_ms: 2100, source: "measured:4 samples", samples: 4 },
  estimated_cost_usd: 0.0,
  creative_risk: "low",
  regression_risk: "low",
  rejected_alternatives: [
    { action: "crop_existing_shot", reason: "Digital zoom would need a 4.1x upscale of the wide shot; below the quality floor." },
    { action: "generate_missing_shot", reason: "An existing close-up already covers the claim; generation costs more and risks changing the product." },
    { action: "revise_captions", reason: "A caption would explain the action but not show it; the failing questions are visual." },
  ],
  requires_generation: false,
  generation_request: null,
  validation_requirements: ["duration within 8000-12000 ms", "narration asset unchanged", "assets authorized"],
  policy_rule_ids_used: ["rule_0001"],
  planner: "gemini:gemini-3.1-pro-preview",
  routing_table: [
    { action: "do_nothing", allowed: true, feasible: true, expected_improvement: "none", latency: { expected_ms: 0, source: "default", samples: 0 }, cost_usd: 0, creative_risk: "low", regression_risk: "low", prior_success_rate: null, prior_samples: 0, note: "supported failure exists" },
    { action: "trim_or_retime", allowed: true, feasible: true, expected_improvement: "low", latency: { expected_ms: 1900, source: "measured:3 samples", samples: 3 }, cost_usd: 0, creative_risk: "low", regression_risk: "medium", prior_success_rate: 0.25, prior_samples: 2, note: "did not help on this failure class before" },
    { action: "replace_with_existing_asset", allowed: true, feasible: true, expected_improvement: "high", latency: { expected_ms: 2100, source: "measured:4 samples", samples: 4 }, cost_usd: 0, creative_risk: "low", regression_risk: "low", prior_success_rate: 0.75, prior_samples: 2, note: "verified close-up covers the claim" },
    { action: "crop_existing_shot", allowed: true, feasible: false, expected_improvement: "medium", latency: { expected_ms: 2000, source: "default", samples: 0 }, cost_usd: 0, creative_risk: "low", regression_risk: "low", prior_success_rate: null, prior_samples: 0, note: "4.1x upscale exceeds the 2.0x floor" },
    { action: "revise_captions", allowed: true, feasible: true, expected_improvement: "low", latency: { expected_ms: 1800, source: "default", samples: 0 }, cost_usd: 0, creative_risk: "low", regression_risk: "low", prior_success_rate: null, prior_samples: 0, note: "visual questions need visual evidence" },
    { action: "generate_missing_shot", allowed: true, feasible: false, expected_improvement: "unknown", latency: { expected_ms: 0, source: "unknown", samples: 0 }, cost_usd: null, creative_risk: "high", regression_risk: "medium", prior_success_rate: null, prior_samples: 0, note: "live generation gate disabled: no benchmark on record" },
  ],
  decision_summary: "The script already says it locks. The failure is visual: the action is hidden in the wide shot. A verified close-up exists, so the cheapest sufficient repair is to cut to it at the locking moment.",
};

const PROPOSAL_TRIM: RepairProposal = {
  ...PROPOSAL_FIXED,
  id: "prop_0001",
  hypothesis: "Trimming the wide shot to the locking moment should make the action larger in relative screen time.",
  action: "trim_or_retime",
  ops: [{ type: "trim_segment", segment_id: "seg_wide", source_in_ms: 1800, source_out_ms: 4000 }],
  predicted_benefit: "Prediction: mechanism questions improve. (Did not hold.)",
  policy_rule_ids_used: [],
  planner: "rules (baseline policy, no memory)",
  rejected_alternatives: [],
  decision_summary: "Baseline policy without memory picked the cheapest action in cost order.",
};

const DECISION_REJECTED: PromotionDecision = {
  outcome: "rejected",
  hard_gates: [
    { id: "render_valid", passed: true, detail: "candidate decoded and published" },
    { id: "mechanical_checks", passed: false, detail: "narration_within_timeline failed; duration_in_bounds failed" },
    { id: "constraints", passed: true, detail: "5/5 protected constraints pass" },
    { id: "assets_authorized", passed: true, detail: "all assets in manifest" },
    { id: "no_critical_regression", passed: false, detail: "q_tap_stable regressed (3/3 to 0/3)" },
  ],
  min_improvement_questions: 1,
  delta_questions: -1,
  delta_score: -0.167,
  regressions: ["q_tap_stable"],
  fixed: [],
  reason: "Mechanical gate failed: the trim cut the video shorter than the narration. Baseline preserved.",
  blocking_gate_id: "mechanical_checks",
};

const DECISION_PROMOTED: PromotionDecision = {
  outcome: "promoted",
  hard_gates: [
    { id: "render_valid", passed: true, detail: "candidate decoded and published (10500 ms)" },
    { id: "mechanical_checks", passed: true, detail: "6/6 critical checks pass" },
    { id: "constraints", passed: true, detail: "5/5 protected constraints pass" },
    { id: "assets_authorized", passed: true, detail: "all assets in manifest" },
    { id: "no_critical_regression", passed: true, detail: "3 guards kept" },
  ],
  min_improvement_questions: 1,
  delta_questions: 3,
  delta_score: 0.5,
  regressions: [],
  fixed: ["q_lock_action", "q_tab_location", "q_moving_part"],
  reason: "3 previously failing questions now pass; no regressions; all gates pass.",
  blocking_gate_id: null,
};

function comparison(base: EvaluationRun, cand: EvaluationRun, baseId: string, candId: string, durBefore: number, durAfter: number): Comparison {
  const deltas = QUESTIONS.map((q) => {
    const b = base.question_summaries.find((s) => s.question_id === q.id)!;
    const c = cand.question_summaries.find((s) => s.question_id === q.id)!;
    return { question_id: q.id, before_passed: b.passed, after_passed: c.passed, before_rate: b.pass_rate, after_rate: c.pass_rate, regression_guard: q.regression_guard };
  });
  return {
    baseline_version_id: baseId,
    candidate_version_id: candId,
    suite_hash: SUITE_HASH,
    matched_config: true,
    config_note: "same provider, model, trials and suite hash",
    deltas,
    fixed: deltas.filter((d) => !d.before_passed && d.after_passed).map((d) => d.question_id),
    regressed: deltas.filter((d) => d.before_passed && !d.after_passed).map((d) => d.question_id),
    kept: deltas.filter((d) => d.before_passed && d.after_passed).map((d) => d.question_id),
    still_failing: deltas.filter((d) => !d.before_passed && !d.after_passed).map((d) => d.question_id),
    score_before: base.score,
    score_after: cand.score,
    questions_passed_before: base.questions_passed,
    questions_passed_after: cand.questions_passed,
    questions_total: QUESTIONS.length,
    duration_before_ms: durBefore,
    duration_after_ms: durAfter,
    mechanical_before: base.mechanical_passed,
    mechanical_after: cand.mechanical_passed,
    constraints_before: base.constraints_passed,
    constraints_after: cand.constraints_passed,
  };
}

interface MockVersion {
  view: VersionView;
  plan: EditPlan;
  run: EvaluationRun | null;
  diff_lines: string[];
}

interface MockJob {
  view: JobView;
  events: JobEvent[];
  listeners: Set<(e: JobEvent) => void>;
  timers: number[];
}

const nowIso = () => new Date().toISOString();

function createState() {
  const versions = new Map<string, MockVersion>();
  versions.set("ver_0", {
    view: {
      id: "ver_0", project_id: PROJECT_ID, index: 0, parent_version_id: null, role: "baseline", status: "baseline",
      artifact_hash: V0_HASH, media_url: "/mock/v0.mp4", duration_ms: 9500, width: 720, height: 1280,
      created_at: "2026-09-12T18:01:40.000Z", evaluation: summary(V0_EVAL), job_id: null,
    },
    plan: BASE_PLAN,
    run: V0_EVAL,
    diff_lines: ["Baseline: creator first cut (wide 0-5500 ms, result 0-4000 ms)."],
  });
  versions.set("ver_1", {
    view: {
      id: "ver_1", project_id: PROJECT_ID, index: 1, parent_version_id: "ver_0", role: "candidate", status: "rejected",
      artifact_hash: V1_HASH, media_url: "/mock/v1.mp4", duration_ms: 6200, width: 720, height: 1280,
      created_at: "2026-09-12T18:05:02.000Z", evaluation: summary(V1_EVAL), job_id: "job_rec_1",
    },
    plan: TRIM_PLAN,
    run: V1_EVAL,
    diff_lines: ["Trimmed seg_wide (wide shot) from 0-5500 ms to 1800-4000 ms."],
  });

  const jobs = new Map<string, MockJob>();
  const recorded: JobView = {
    id: "job_rec_1", project_id: PROJECT_ID, kind: "improve", state: "COMPLETED", stage: "DONE", iteration: 1,
    base_version_id: "ver_0", candidate_version_id: "ver_1", created_at: "2026-09-12T18:04:40.000Z",
    started_at: "2026-09-12T18:04:41.000Z", ended_at: "2026-09-12T18:05:09.300Z", elapsed_ms: 28300, deadline_ms: 45000,
    error: null, mode: "recorded",
    result: {
      finding: FINDING, proposal: PROPOSAL_TRIM, decision: DECISION_REJECTED,
      comparison: comparison(V0_EVAL, V1_EVAL, "ver_0", "ver_1", 9500, 6200),
      diff_lines: ["Trimmed seg_wide (wide shot) from 0-5500 ms to 1800-4000 ms."],
      timings_ms: { preflight: 210, evaluate_baseline: 0, diagnose: 1450, plan: 3900, render: 1900, reevaluate: 6800, regression: 40, decide: 20, learn: 60 },
      weave_url: null, weave_call_id: null,
      policy_update: { rule_id: "rule_0001", status: "proposed", support: 0, counter: 1 },
    },
  };
  const recordedEvents: JobEvent[] = [
    { seq: 1, ts: "2026-09-12T18:04:41.000Z", stage: "PREFLIGHT", message: "Pack verified, budget reserved (0.00 of 2.00 USD)", data: null },
    { seq: 2, ts: "2026-09-12T18:04:41.300Z", stage: "EVALUATING_BASELINE", message: "Baseline evaluation reused: same artifact and suite hash (CACHED)", data: { mode: "cached" } },
    { seq: 3, ts: "2026-09-12T18:04:42.700Z", stage: "DIAGNOSING", message: "MISSING_ACTION_VISIBILITY on seg_wide (9/9 mechanism trials not shown)", data: { finding: FINDING } },
    { seq: 4, ts: "2026-09-12T18:04:46.600Z", stage: "PLANNING", message: "Baseline policy: trim_or_retime", data: { proposal: PROPOSAL_TRIM } },
    { seq: 5, ts: "2026-09-12T18:04:48.500Z", stage: "RENDERING", message: "Rendered 6200 ms at 720x1280 in 1900 ms", data: null },
    { seq: 6, ts: "2026-09-12T18:04:55.300Z", stage: "REEVALUATING", message: "6 questions x 3 trials, gemini-3.8-flash, video_native", data: null },
    { seq: 7, ts: "2026-09-12T18:04:55.340Z", stage: "REGRESSION_TESTING", message: "q_tap_stable regressed", data: null },
    { seq: 8, ts: "2026-09-12T18:04:55.360Z", stage: "DECIDING", message: "REJECTED: mechanical_checks gate failed", data: { decision: DECISION_REJECTED } },
    { seq: 9, ts: "2026-09-12T18:04:55.420Z", stage: "LEARNING", message: "Counterexample recorded on rule_0001", data: null },
    { seq: 10, ts: "2026-09-12T18:05:09.300Z", stage: "DONE", message: "Baseline preserved", data: null },
  ];
  jobs.set("job_rec_1", { view: recorded, events: recordedEvents, listeners: new Set(), timers: [] });

  const idempotency = new Map<string, string>();
  const policies: PolicyStore = {
    version: 3,
    rules: [
      {
        id: "rule_0001", version: 1, profile_scope: ["product_demo", "educational"],
        trigger_category: "MISSING_ACTION_VISIBILITY",
        trigger_condition: "Visual mechanism probes fail while the transcript does not describe the mechanism and a closer existing asset covers the claim",
        recommended_action: "replace_with_existing_asset",
        action_detail: "Cut to the closer source shot at the moment the action happens; leave narration untouched.",
        contraindications: ["The action is intentionally withheld for a reveal", "No closer asset verified to show the claim"],
        supporting: [
          { experiment_id: "job_dev_7", project_id: "proj_dev_b", story_family: "latch_demo", split: "dev", outcome: "promoted", delta_questions: 2, delta_score: 0.33, regressions: 0, note: "" },
        ],
        counterexamples: [
          { experiment_id: "job_rec_1", project_id: PROJECT_ID, story_family: "stand_demo", split: "dev", outcome: "rejected", delta_questions: -1, delta_score: -0.167, regressions: 1, note: "trim_or_retime attempt (baseline policy) failed mechanically" },
        ],
        status: "proposed", confidence: 0.55,
        created_at: "2026-09-12T17:30:00.000Z", updated_at: "2026-09-12T18:05:09.000Z", retired_reason: null,
        probe_config: "gemini/gemini-3.8-flash x3",
      },
    ],
    router_stats: [
      { profile: "product_demo", category: "MISSING_ACTION_VISIBILITY", action: "trim_or_retime", attempts: 2, successes: 0, total_latency_ms: 3800, total_cost_usd: 0 },
      { profile: "product_demo", category: "MISSING_ACTION_VISIBILITY", action: "replace_with_existing_asset", attempts: 2, successes: 2, total_latency_ms: 4200, total_cost_usd: 0 },
    ],
  };

  return { versions, jobs, idempotency, policies };
}

export function createMockClient(): ApiClient {
  const state = createState();
  let jobCounter = 0;
  let versionCounter = 2;

  const projectSummary = (): ProjectSummary => ({
    id: PROJECT_ID,
    name: "Cereal-box phone stand",
    profile: "product_demo",
    pack_label: "Pack A: rendered fixture (fictional graphical demonstration)",
    version_count: state.versions.size,
    best_version_id: [...state.versions.values()].find((v) => v.view.status === "promoted")?.view.id ?? "ver_0",
    created_at: "2026-09-12T18:01:00.000Z",
  });

  const projectDetail = (): ProjectDetail => ({
    ...projectSummary(),
    brief: BRIEF,
    truth: { title: "Cereal-box phone stand with a locking tab", is_fictional: true, claim_count: 6, summary: "A cardboard stand locks when its orange tab folds into a slot in the base; the phone then stays put when tapped." },
    assets: ASSETS,
    versions: [...state.versions.values()].map((v) => v.view),
    suite: { id: "suite_stand_demo", hash: SUITE_HASH, split: "dev", story_family: "stand_demo", question_count: QUESTIONS.length },
    provenance_note: "Mock mode. Procedurally rendered fixture, fictional, no real product. Nothing shown here is a measurement.",
  });

  function emit(job: MockJob, stage: string, message: string, data: Record<string, unknown> | null) {
    const event: JobEvent = { seq: job.events.length + 1, ts: nowIso(), stage, message, data };
    job.events.push(event);
    job.view.stage = stage;
    job.listeners.forEach((fn) => fn(event));
  }

  function elapsed(job: JobView): number {
    if (!job.started_at) return 0;
    const end = job.ended_at ? Date.parse(job.ended_at) : Date.now();
    return Math.max(0, end - Date.parse(job.started_at));
  }

  function finish(job: MockJob, state_: JobView["state"], error: string | null) {
    job.view.state = state_;
    job.view.error = error;
    job.view.ended_at = nowIso();
    job.view.elapsed_ms = elapsed(job.view);
    job.timers.forEach((t) => window.clearTimeout(t));
    job.timers = [];
  }

  function runImprove(job: MockJob) {
    const schedule = (ms: number, fn: () => void) => {
      job.timers.push(window.setTimeout(fn, ms));
    };
    job.view.started_at = nowIso();
    job.view.state = "PREFLIGHT";
    schedule(300, () => emit(job, "PREFLIGHT", "Pack verified, suite frozen (hash 9c1f2a7e), budget reserved (0.00 of 2.00 USD)", null));
    schedule(800, () => {
      job.view.state = "EVALUATING";
      emit(job, "EVALUATING_BASELINE", "Baseline evaluation reused: same artifact hash and suite hash (CACHED, evaluated 18:02:17)", { mode: "cached" });
    });
    schedule(1600, () => {
      job.view.state = "DIAGNOSING";
      emit(job, "DIAGNOSING", "MISSING_ACTION_VISIBILITY on seg_wide: 9 of 9 mechanism trials answered not shown; guards 9/9", { finding: FINDING });
    });
    schedule(3200, () => {
      job.view.state = "PLANNING";
      emit(job, "PLANNING", "replace_with_existing_asset: cut to the verified close-up at 2000-4500 ms; narration unchanged", { proposal: PROPOSAL_FIXED });
    });
    schedule(4800, () => {
      job.view.state = "RENDERING";
      emit(job, "RENDERING", "ffmpeg rendered 10500 ms at 720x1280 (mock timing)", null);
    });
    schedule(5200, () => {
      job.view.state = "REEVALUATING";
      emit(job, "REEVALUATING", "6 questions x 3 trials, gemini-3.8-flash, video_native, fresh", null);
    });
    schedule(7000, () => {
      job.view.state = "REGRESSION_TESTING";
      emit(job, "REGRESSION_TESTING", "fixed 3, kept 3, regressed 0", null);
    });
    schedule(7600, () => {
      job.view.state = "DECIDING";
      emit(job, "DECIDING", "PROMOTED: all hard gates pass, +3 questions", { decision: DECISION_PROMOTED });
    });
    schedule(7900, () => {
      const candId = `ver_${versionCounter}`;
      versionCounter += 1;
      const run = evalRun(`eval_${candId}`, candId, V2_HASH, FIXED_PASSED, "fresh", MECH_OK.map((m) => (m.id === "duration_in_bounds" ? { ...m, value: 10500, detail: "10500 ms" } : m)));
      const diff = [
        "Trimmed seg_wide (wide shot) from 0-5500 ms to 0-2000 ms.",
        "Inserted close-up 0-2500 ms after seg_wide.",
        "Inserted wide shot 3500-5500 ms after seg_closeup.",
      ];
      state.versions.set(candId, {
        view: {
          id: candId, project_id: PROJECT_ID, index: versionCounter - 1, parent_version_id: job.view.base_version_id, role: "candidate", status: "promoted",
          artifact_hash: V2_HASH, media_url: "/mock/v1.mp4", duration_ms: 10500, width: 720, height: 1280,
          created_at: nowIso(), evaluation: summary(run), job_id: job.view.id,
        },
        plan: FIXED_PLAN,
        run,
        diff_lines: diff,
      });
      job.view.candidate_version_id = candId;
      job.view.result = {
        finding: FINDING, proposal: PROPOSAL_FIXED, decision: DECISION_PROMOTED,
        comparison: comparison(V0_EVAL, run, "ver_0", candId, 9500, 10500),
        diff_lines: diff,
        timings_ms: { preflight: 300, evaluate_baseline: 0, diagnose: 800, plan: 1600, render: 1600, reevaluate: 1800, regression: 600, decide: 600, learn: 300 },
        weave_url: null, weave_call_id: null,
        policy_update: { rule_id: "rule_0001", status: "supported_on_dev", support: 2, counter: 1 },
      };
      const rule = state.policies.rules[0];
      rule.supporting.push({ experiment_id: job.view.id, project_id: PROJECT_ID, story_family: "stand_demo", split: "dev", outcome: "promoted", delta_questions: 3, delta_score: 0.5, regressions: 0, note: "" });
      rule.status = "supported_on_dev";
      rule.confidence = 0.68;
      rule.updated_at = nowIso();
      state.policies.version += 1;
      emit(job, "LEARNING", "rule_0001 now supported_on_dev (support 2, counter 1)", { policy_update: job.view.result.policy_update });
    });
    schedule(8100, () => {
      finish(job, "COMPLETED", null);
      emit(job, "DONE", "Candidate promoted", null);
    });
  }

  const delay = <T,>(value: T, ms = 120): Promise<T> => new Promise((resolve) => window.setTimeout(() => resolve(value), ms));

  const client: ApiClient = {
    mode: "mock",
    healthReady: () =>
      delay<HealthReady>({
        status: "degraded",
        db: true,
        ffmpeg: "mock",
        providers: { gemini: { present: true, smoke: "untested" }, openai: { present: true, smoke: "untested" }, wandb_inference: { present: false, smoke: "untested" }, typesafe: { present: false, smoke: "untested" }, elevenlabs: { present: true, smoke: "untested" } },
        weave: { enabled: true, project: "leondragon3798-curio/directorloop", connected: false, reason: "mock mode" },
        mode: "demo",
      }),
    listProjects: () => delay([projectSummary()]),
    importProject: () => delay(projectDetail()),
    getProject: (id) => (id === PROJECT_ID ? delay(projectDetail()) : Promise.reject(new ApiError(404, "project not found"))),
    listVersions: () => delay([...state.versions.values()].map((v) => v.view)),
    getVersion: (id) => {
      const v = state.versions.get(id);
      if (!v) return Promise.reject(new ApiError(404, "version not found"));
      const detail: VersionDetail = { ...v.view, plan: v.plan, evaluation_run: v.run, diff_lines: v.diff_lines, plan_diff: null, questions: QUESTIONS };
      return delay(detail);
    },
    getComparison: (versionId, against) => {
      const a = state.versions.get(against);
      const b = state.versions.get(versionId);
      if (!a?.run || !b?.run) return Promise.reject(new ApiError(404, "comparison unavailable"));
      return delay(comparison(a.run, b.run, against, versionId, a.view.duration_ms, b.view.duration_ms));
    },
    approveVersion: (id) => {
      const v = state.versions.get(id);
      if (!v) return Promise.reject(new ApiError(404, "version not found"));
      return delay(v.view);
    },
    rejectVersion: (id) => {
      const v = state.versions.get(id);
      if (!v) return Promise.reject(new ApiError(404, "version not found"));
      return delay(v.view);
    },
    startBaselineJob: () => Promise.reject(new ApiError(409, "baseline already exists for this project")),
    startImprovementJob: (projectId, baseVersionId, idempotencyKey) => {
      const existing = state.idempotency.get(idempotencyKey);
      if (existing) return delay(state.jobs.get(existing)!.view);
      jobCounter += 1;
      const id = `job_live_${jobCounter}`;
      const view: JobView = {
        id, project_id: projectId, kind: "improve", state: "QUEUED", stage: "QUEUED", iteration: 1,
        base_version_id: baseVersionId ?? "ver_0", candidate_version_id: null, created_at: nowIso(), started_at: null,
        ended_at: null, elapsed_ms: 0, deadline_ms: 45000, error: null, mode: "live", result: null,
      };
      const job: MockJob = { view, events: [], listeners: new Set(), timers: [] };
      state.jobs.set(id, job);
      state.idempotency.set(idempotencyKey, id);
      window.setTimeout(() => runImprove(job), 200);
      return delay(view, 60);
    },
    getJob: (id) => {
      const job = state.jobs.get(id);
      if (!job) return Promise.reject(new ApiError(404, "job not found"));
      job.view.elapsed_ms = elapsed(job.view);
      return delay({ ...job.view }, 40);
    },
    listJobs: () => delay([...state.jobs.values()].map((j) => ({ ...j.view, elapsed_ms: elapsed(j.view) })).reverse()),
    cancelJob: (id) => {
      const job = state.jobs.get(id);
      if (!job) return Promise.reject(new ApiError(404, "job not found"));
      if (!isTerminal(job.view.state)) {
        finish(job, "CANCELED", "canceled by operator");
        emit(job, "DONE", "Canceled by operator; baseline unchanged", null);
      }
      return delay({ ...job.view });
    },
    streamJobEvents: (jobId, afterSeq, onEvent, signal) =>
      new Promise<void>((resolve, reject) => {
        const job = state.jobs.get(jobId);
        if (!job) {
          reject(new ApiError(404, "job not found"));
          return;
        }
        job.events.filter((e) => e.seq > afterSeq).forEach(onEvent);
        if (isTerminal(job.view.state)) {
          resolve();
          return;
        }
        const listener = (e: JobEvent) => {
          onEvent(e);
          if (isTerminal(job.view.state) && e.stage === "DONE") {
            job.listeners.delete(listener);
            resolve();
          }
        };
        job.listeners.add(listener);
        signal.addEventListener("abort", () => {
          job.listeners.delete(listener);
          resolve();
        });
      }),
    getPolicies: () => delay(JSON.parse(JSON.stringify(state.policies)) as PolicyStore),
    getBenchmarks: () =>
      delay<BenchmarkRun[]>([
        {
          id: "bench_mock_1", ran_at: "2026-09-12T19:00:00.000Z", environment: "mock (no GPU)", gpu: null, vram_gb: null,
          model: "Lightricks/LTX-Video-0.9.8-distilled", profile: "1s 512x896 8 steps", output_seconds: 1, width: 512, height: 896, frames: 25,
          cold_load_ms: null, warm_infer_ms: [], encode_ms: [], end_to_end_ms: [], browser_first_frame_ms: null, peak_vram_gb: null,
          failures: 0, samples: 0, p50_ms: null, p95_ms: null,
          quality_note: "No measurement in mock mode. Run notebooks/gpu_benchmark.py on Molab and import the result.",
          source: "imported", live_gate: "untested",
        },
      ]),
    getProviders: () =>
      delay<ProviderEntry[]>([
        { name: "gemini", role: "media probe (native video), planner", present: true, state: "unknown", modalities: ["text", "image", "video", "audio"], model: "gemini-3.8-flash", smoke_result: null, checked_at: null },
        { name: "openai", role: "planner, frame-based probe", present: true, state: "unknown", modalities: ["text", "image"], model: "gpt-5.6-terra", smoke_result: null, checked_at: null },
        { name: "wandb_inference", role: "hosted planner / frame-based probe", present: false, state: "unknown", modalities: ["text", "image"], model: "Qwen/Qwen3.8-27B", smoke_result: null, checked_at: null },
        { name: "typesafe", role: "disabled until the event contract is verified", present: false, state: "unknown", modalities: [], model: null, smoke_result: null, checked_at: null },
        { name: "elevenlabs", role: "speech (fixture narration)", present: true, state: "unknown", modalities: ["audio"], model: "eleven_v3", smoke_result: null, checked_at: null },
        { name: "ltx_local", role: "GPU shot generation (Molab)", present: false, state: "unknown", modalities: ["video"], model: "Lightricks/LTX-Video-0.9.8-distilled", smoke_result: null, checked_at: null },
      ]),
    createReviewSession: () => delay({ token: "mock-review-token", url: "/review/mock-review-token" }),
    getReview: (token) => {
      if (token !== "mock-review-token") return Promise.reject(new ApiError(404, "review session not found"));
      const assignment: ReviewAssignment = {
        assignment: "A",
        media_url: "/mock/v1.mp4",
        consent_text: "You are helping test whether a short video communicates its content. Your answers are stored with a pseudonymous id and a timestamp only. You may stop at any time.",
        questions: QUESTIONS.map((q) => ({
          ...q,
          options: [
            { id: `opt_${q.id}_a`, text: "Option A" },
            { id: `opt_${q.id}_b`, text: "Option B" },
            { id: `opt_${q.id}_c`, text: "Option C" },
            { id: "not_shown", text: "Not shown / cannot tell" },
          ],
        })),
      };
      return delay(assignment);
    },
    submitReview: (_token, _body: ReviewAnswers) => delay({ recorded: true }),
    getReviewSummary: () =>
      delay<ReviewSummary>({
        participants: 0, responses: 0, per_version: {},
        procedure: "Blinded single-version comprehension; random assignment; pseudonymous participant id.",
        limitations: "No human responses recorded in mock mode.",
      }),
  };
  return client;
}
