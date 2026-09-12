// Mirrors docs/API.md (v1) and the Python domain contracts in directorloop/domain.

export type GoalProfile = "educational" | "product_demo" | "narrative" | "comedy" | "cinematic";

export type RepairAction =
  | "do_nothing"
  | "trim_or_retime"
  | "reorder_segments"
  | "replace_with_existing_asset"
  | "crop_existing_shot"
  | "revise_captions"
  | "revise_narration"
  | "add_graphic"
  | "generate_missing_shot"
  | "regenerate_segment"
  | "regenerate_full_draft";

export type ConstraintKind =
  | "keep_narration"
  | "keep_music"
  | "no_new_text"
  | "preserve_interval"
  | "max_duration_ms"
  | "min_duration_ms"
  | "no_generation"
  | "preserve_product_appearance"
  | "no_new_facts"
  | "keep_silence_interval"
  | "no_cta";

export interface ProtectedConstraint {
  id: string;
  kind: ConstraintKind;
  reason: string;
  start_ms: number | null;
  end_ms: number | null;
  value_ms: number | null;
}

export interface RequiredInformation {
  id: string;
  text: string;
  claim_ids: string[];
}

export interface Budget {
  max_usd: number;
  max_llm_calls: number;
  max_generation_calls: number;
  max_wall_seconds: number;
}

export interface CreativeBrief {
  id: string;
  project_id: string;
  profile: GoalProfile;
  objective: string;
  audience: string;
  language: string;
  aspect_ratio: string;
  target_duration_ms_min: number;
  target_duration_ms_max: number;
  required_information: RequiredInformation[];
  style_notes: string;
  protected_constraints: ProtectedConstraint[];
  allowed_actions: RepairAction[];
  generation_permitted: boolean;
  narration_change_permitted: boolean;
  budget: Budget;
  review_status: string;
  approved_by: string | null;
}

// --- Edit plan --------------------------------------------------------------

export interface OutputProfile {
  width: number;
  height: number;
  fps_num: number;
  fps_den: number;
}

export interface CropRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Segment {
  id: string;
  asset_id: string;
  source_in_ms: number;
  source_out_ms: number;
  fit: "contain" | "cover";
  crop: CropRect | null;
  speed: number;
  audio_policy: "keep" | "mute";
  label: string;
}

export interface Caption {
  id: string;
  text: string;
  start_ms: number;
  end_ms: number;
  position: "top" | "middle" | "bottom";
  style: string;
}

export interface EditPlan {
  schema_version: string;
  parent_plan_hash: string | null;
  output: OutputProfile;
  segments: Segment[];
  captions: Caption[];
  narration: { asset_id: string; offset_ms: number; gain_db: number } | null;
  music: { asset_id: string; gain_db: number } | null;
  protected_intervals: { start_ms: number; end_ms: number; reason: string }[];
  change_rationale: string;
}

export interface EditOp {
  type: string;
  [key: string]: unknown;
}

// --- Evaluation ----------------------------------------------------------

export type ProbeModality = "video_native" | "frames_and_transcript" | "transcript_only" | "none";
export type MeasurementType = "mechanical" | "model_probe" | "human";
export type RunMode = "fresh" | "cached" | "recorded";

export interface MechanicalCheck {
  id: string;
  passed: boolean;
  severity: "critical" | "warning";
  value: number | string | null;
  threshold: number | string | null;
  detail: string;
  measurement_type: MeasurementType;
}

export interface ConstraintCheck {
  constraint_id: string;
  kind: string;
  passed: boolean;
  detail: string;
}

export interface ProbeResult {
  question_id: string;
  trial: number;
  chosen_option_id: string | null;
  correct: boolean | null;
  error: string | null;
  latency_ms: number;
}

export interface QuestionSummary {
  question_id: string;
  modality: string;
  weight: number;
  regression_guard: boolean;
  valid_trials: number;
  errors: number;
  correct: number;
  pass_rate: number;
  passed: boolean;
  chosen: Record<string, number>;
}

export interface EvaluationRun {
  id: string;
  version_id: string;
  artifact_hash: string;
  suite_id: string;
  suite_hash: string;
  mode: RunMode;
  probe_modality: ProbeModality;
  provider: string;
  model: string;
  trials: number;
  frames_sampled: number | null;
  frame_timestamps_ms: number[];
  transcript_source: string | null;
  transcript_text: string | null;
  results: ProbeResult[];
  question_summaries: QuestionSummary[];
  mechanical: MechanicalCheck[];
  constraints: ConstraintCheck[];
  questions_passed: number;
  questions_total: number;
  trials_correct: number;
  trials_valid: number;
  trials_errored: number;
  score: number;
  mechanical_passed: boolean;
  constraints_passed: boolean;
  started_at: string | null;
  ended_at: string | null;
  latency_ms: number;
  probe_latency_ms: number;
  cost_usd_estimate: number | null;
  weave_call_id: string | null;
  weave_url: string | null;
  cache_key: string | null;
  notes: string[];
}

export interface EvaluationSummary {
  run_id: string;
  mode: RunMode;
  probe_modality: string;
  provider: string;
  model: string;
  trials: number;
  questions_passed: number;
  questions_total: number;
  score: number;
  mechanical_passed: boolean;
  constraints_passed: boolean;
  failed_question_ids: string[];
  latency_ms: number;
  weave_url: string | null;
}

// --- Findings, repair, decision -----------------------------------------

export interface FindingEvidence {
  failed_question_ids: string[];
  probe_failures: number;
  probe_trials: number;
  mechanical_check_ids: string[];
  constraint_ids: string[];
  claim_ids: string[];
  transcript_mentions_claim: boolean | null;
  coverage_note: string;
}

export interface FailureFinding {
  id: string;
  category: string;
  severity: number;
  confidence: number;
  start_ms: number | null;
  end_ms: number | null;
  timestamp_precision: string;
  observed: string;
  inferred_cause: string;
  alternatives: string[];
  evidence: FindingEvidence;
  measurement_type: MeasurementType;
  editable_dimensions: RepairAction[];
  affected_segment_ids: string[];
}

export interface LatencyEstimate {
  expected_ms: number;
  source: string;
  samples: number;
}

export interface ActionEstimate {
  action: RepairAction;
  allowed: boolean;
  feasible: boolean;
  expected_improvement: string;
  latency: LatencyEstimate;
  cost_usd: number | null;
  creative_risk: string;
  regression_risk: string;
  prior_success_rate: number | null;
  prior_samples: number;
  note: string;
}

export interface GenerationRequest {
  prompt: string;
  duration_ms: number;
  width: number;
  height: number;
  conditioning_asset_id: string | null;
  must_show: string[];
  identity_notes: string;
}

export interface RepairProposal {
  id: string;
  finding_id: string;
  hypothesis: string;
  evidence_refs: string[];
  action: RepairAction;
  ops: EditOp[];
  predicted_benefit: string;
  expected_latency: LatencyEstimate;
  estimated_cost_usd: number;
  creative_risk: string;
  regression_risk: string;
  rejected_alternatives: { action: RepairAction; reason: string }[];
  requires_generation: boolean;
  generation_request: GenerationRequest | null;
  validation_requirements: string[];
  policy_rule_ids_used: string[];
  planner: string;
  routing_table: ActionEstimate[];
  decision_summary: string;
}

export type Outcome =
  | "promoted"
  | "rejected"
  | "needs_review"
  | "no_gain"
  | "needs_source_material"
  | "insufficient_evidence";

export interface HardGate {
  id: string;
  passed: boolean;
  detail: string;
}

export interface PromotionDecision {
  outcome: Outcome;
  hard_gates: HardGate[];
  min_improvement_questions: number;
  delta_questions: number;
  delta_score: number;
  regressions: string[];
  fixed: string[];
  reason: string;
  blocking_gate_id: string | null;
}

export interface QuestionDelta {
  question_id: string;
  before_passed: boolean;
  after_passed: boolean;
  before_rate: number;
  after_rate: number;
  regression_guard: boolean;
}

export interface Comparison {
  baseline_version_id: string;
  candidate_version_id: string;
  suite_hash: string;
  matched_config: boolean;
  config_note: string;
  deltas: QuestionDelta[];
  fixed: string[];
  regressed: string[];
  kept: string[];
  still_failing: string[];
  score_before: number;
  score_after: number;
  questions_passed_before: number;
  questions_passed_after: number;
  questions_total: number;
  duration_before_ms: number;
  duration_after_ms: number;
  mechanical_before: boolean;
  mechanical_after: boolean;
  constraints_before: boolean;
  constraints_after: boolean;
}

// --- Policy ----------------------------------------------------------------

export type PolicyStatus =
  | "proposed"
  | "supported_on_dev"
  | "validated_on_other_stories"
  | "human_supported"
  | "contradicted"
  | "rejected"
  | "retired";

export interface PolicyEvidence {
  experiment_id: string;
  project_id: string;
  story_family: string;
  split: string;
  outcome: string;
  delta_questions: number;
  delta_score: number;
  regressions: number;
  note: string;
}

export interface PolicyRule {
  id: string;
  version: number;
  profile_scope: GoalProfile[];
  trigger_category: string;
  trigger_condition: string;
  recommended_action: RepairAction;
  action_detail: string;
  contraindications: string[];
  supporting: PolicyEvidence[];
  counterexamples: PolicyEvidence[];
  status: PolicyStatus;
  confidence: number;
  created_at: string;
  updated_at: string;
  retired_reason: string | null;
  probe_config: string;
}

export interface RouterStat {
  profile: GoalProfile;
  category: string;
  action: RepairAction;
  attempts: number;
  successes: number;
  total_latency_ms: number;
  total_cost_usd: number;
}

export interface PolicyStore {
  version: number;
  rules: PolicyRule[];
  router_stats: RouterStat[];
}

// --- Projects, versions, jobs -------------------------------------------

export interface ProjectSummary {
  id: string;
  name: string;
  profile: GoalProfile;
  pack_label: string;
  version_count: number;
  best_version_id: string | null;
  created_at: string;
}

export interface AssetView {
  id: string;
  label: string;
  kind: "video" | "audio" | "image" | "graphic";
  origin: string;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  media_url: string | null;
  content_hash: string;
}

export interface VersionView {
  id: string;
  project_id: string;
  index: number;
  parent_version_id: string | null;
  role: "baseline" | "candidate";
  status: "baseline" | "promoted" | "rejected" | "needs_review" | "no_gain" | "unevaluated";
  artifact_hash: string;
  media_url: string;
  duration_ms: number;
  width: number;
  height: number;
  created_at: string;
  evaluation: EvaluationSummary | null;
  job_id: string | null;
}

export interface QuestionView {
  id: string;
  text: string;
  modality: "visual" | "audio" | "either";
  regression_guard: boolean;
  leakage_status: string;
}

export interface ProjectDetail extends ProjectSummary {
  brief: CreativeBrief;
  truth: { title: string; is_fictional: boolean; claim_count: number; summary: string };
  assets: AssetView[];
  versions: VersionView[];
  suite: { id: string; hash: string; split: string; story_family: string; question_count: number };
  provenance_note: string;
}

export interface VersionDetail extends VersionView {
  plan: EditPlan;
  evaluation_run: EvaluationRun | null;
  diff_lines: string[];
  plan_diff: Record<string, unknown> | null;
  questions: QuestionView[];
}

export type JobState =
  | "QUEUED"
  | "PREFLIGHT"
  | "ANALYZING"
  | "EVALUATING"
  | "DIAGNOSING"
  | "PLANNING"
  | "GENERATING"
  | "RENDERING"
  | "REEVALUATING"
  | "REGRESSION_TESTING"
  | "DECIDING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELED"
  | "TIMED_OUT"
  | "NEEDS_REVIEW"
  | "NEEDS_SOURCE_MATERIAL"
  | "NO_GAIN";

export const TERMINAL_STATES: JobState[] = [
  "COMPLETED",
  "FAILED",
  "CANCELED",
  "TIMED_OUT",
  "NEEDS_REVIEW",
  "NEEDS_SOURCE_MATERIAL",
  "NO_GAIN",
];

export function isTerminal(state: JobState): boolean {
  return TERMINAL_STATES.includes(state);
}

export interface JobResult {
  finding: FailureFinding | null;
  proposal: RepairProposal | null;
  decision: PromotionDecision | null;
  comparison: Comparison | null;
  diff_lines: string[];
  timings_ms: Record<string, number>;
  weave_url: string | null;
  weave_call_id: string | null;
  policy_update: { rule_id: string; status: string; support: number; counter: number } | null;
}

export interface JobView {
  id: string;
  project_id: string;
  kind: "baseline" | "improve";
  state: JobState;
  stage: string;
  iteration: number;
  base_version_id: string | null;
  candidate_version_id: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  elapsed_ms: number;
  deadline_ms: number;
  error: string | null;
  mode: "live" | "recorded";
  result: JobResult | null;
}

export interface JobEvent {
  seq: number;
  ts: string;
  stage: string;
  message: string;
  data: Record<string, unknown> | null;
}

// --- Benchmarks, providers, review, health ---------------------------------

export interface BenchmarkRun {
  id: string;
  ran_at: string;
  environment: string;
  gpu: string | null;
  vram_gb: number | null;
  model: string;
  profile: string;
  output_seconds: number;
  width: number;
  height: number;
  frames: number;
  cold_load_ms: number | null;
  warm_infer_ms: number[];
  encode_ms: number[];
  end_to_end_ms: number[];
  browser_first_frame_ms: number[] | null;
  peak_vram_gb: number | null;
  failures: number;
  samples: number;
  p50_ms: number | null;
  p95_ms: number | null;
  quality_note: string;
  source: "measured" | "imported";
  live_gate: "enabled" | "disabled" | "untested";
}

export interface ProviderEntry {
  name: string;
  role: string;
  present: boolean;
  state: "unknown" | "verified_supported" | "verified_unsupported";
  modalities: string[];
  model: string | null;
  smoke_result: string | null;
  checked_at: string | null;
}

export interface ReviewQuestion extends QuestionView {
  options: { id: string; text: string }[];
}

export interface ReviewAssignment {
  assignment: "A" | "B";
  media_url: string;
  questions: ReviewQuestion[];
  consent_text: string;
}

export interface ReviewSummary {
  participants: number;
  responses: number;
  per_version: Record<string, unknown>;
  procedure: string;
  limitations: string;
}

export interface HealthReady {
  status: "ok" | "degraded";
  db: boolean;
  ffmpeg: string;
  providers: Record<string, { present: boolean; smoke: "ok" | "untested" | "failed" }>;
  weave: { enabled: boolean; project: string; connected: boolean; reason?: string };
  mode: "demo" | "production";
}
