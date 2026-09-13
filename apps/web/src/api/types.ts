// Mirrors docs/API_CREATIVE.md (v2, experiment-centric) and the cold-audience audit and repair contract.
// Times are ISO-8601 UTC strings; durations are integer milliseconds.

export type EvidenceClass = "real" | "historical" | "simulated" | "model_eval" | "human_test" | "mechanical" | "reference";

// ---------------------------------------------------------------------------------------------
// Videos
// ---------------------------------------------------------------------------------------------

export interface VideoSummary {
  video_id: string;
  title: string;
  duration_ms: number;
  category: string;
  source: string;
  media_url: string;
  role: "demo_a" | "demo_b" | "reference" | "holdout" | "dev" | "upload" | "link" | string;
  has_genome: boolean;
  retention_class: "real" | "historical" | "simulated" | null;
  latest_experiment_id: string | null;
  /** owned (built-in demo), owner_upload (uploaded here) or requires_owner_confirmation (came from a link). */
  edit_permission?: string;
  platform?: string | null;
}

export type FeatureSource = "mechanical" | "asr" | "vision_model" | "language_model" | "creator_declared" | "derived_from_plan";

export interface Feature {
  value: number | string | boolean | null;
  source: FeatureSource;
  confidence: number;
  note: string;
}

export type BeatRole = "hook" | "setup" | "context" | "problem" | "tension" | "proof" | "mechanism" | "payoff" | "cta" | "other";

export interface Beat {
  id: string;
  index: number;
  start_ms: number;
  end_ms: number;
  text: string;
  role: BeatRole;
  role_confidence: number;
  is_question: boolean;
  is_claim: boolean;
  redundant_with_beat_id: string | null;
}

export interface HypothesisEvidence {
  kind: string;
  text: string;
  source: string | null;
  ref: string | null;
}

export interface Hypothesis {
  id: string;
  family: string;
  statement: string;
  region_start_ms: number;
  region_end_ms: number;
  region_basis: string;
  detection_confidence: number;
  evidence: HypothesisEvidence[];
  counterevidence: { kind: string; text: string }[];
  candidate_mutations: string[];
  changed_variable: string;
}

export interface Genome {
  artifact_hash: string;
  duration_ms: number;
  beats: Beat[];
  shots: { id: string; start_ms: number; end_ms: number }[];
  hook: Record<string, Feature>;
  first_proof_ms: Feature;
  first_payoff_ms: Feature;
  context_before_proof_ms: Feature;
  longest_static_span_ms: Feature;
  longest_static_span_start_ms: Feature;
  shots_per_10s: Feature;
  speech_rate_wps: Feature;
  open_loops: { opened_ms: number; resolved_ms: number | null; description: string; confidence: number }[];
  motion_curve: [number, number][];
}

export interface WeakRegion {
  start_ms: number;
  end_ms: number;
  basis: string;
  evidence_class: string;
  description: string;
}

export interface Investigation {
  weak_region: WeakRegion | null;
  hypotheses: Hypothesis[];
  retention_note: string;
}

export interface RetentionSeries {
  source_type: string;
  platform: string;
  points: { t_ms: number; remaining_fraction: number }[];
  avg_watch_time_ms: number | null;
  hold_3s: number | null;
  plays: number | null;
  caveats: string[];
}

export interface VideoDetail extends VideoSummary {
  genome: Genome | null;
  investigation: Investigation | null;
  retention: RetentionSeries | null;
}

export interface Ranking {
  mutation: string;
  hypothesis_id: string;
  family: string;
  score: number;
  detection_confidence: number;
  reference_support: number | null;
  policy_mean: number | null;
  policy_wins: number;
  policy_losses: number;
  policy_neutral: number;
  reason: string;
  description: string;
  changed_variable: string;
  protected_variables: string[];
  target_start_ms: number;
  target_end_ms: number;
  selected: boolean;
}

export type PolicyMode = "learned" | "none";

export interface DesignPreview {
  video_id: string;
  mode: PolicyMode;
  policy_version: number;
  rankings: Ranking[];
  skipped: string[];
}

// ---------------------------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------------------------

export interface ExperimentRequest {
  video_id: string;
  policy_mode: PolicyMode;
  max_arms: number;
  record_policy: boolean;
  idempotency_key: string;
}

export type JobState = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELED";

export const TERMINAL_JOB_STATES: JobState[] = ["COMPLETED", "FAILED", "CANCELED"];

export function isTerminal(state: JobState): boolean {
  return TERMINAL_JOB_STATES.includes(state);
}

export interface Job {
  id: string;
  kind: "experiment" | "run" | string;
  state: JobState;
  stage: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  elapsed_ms: number;
  error: string | null;
  experiment_id: string | null;
  run_id?: string | null;
  causal_id?: string | null;
  params: Record<string, unknown>;
  cancel_requested?: boolean;
}

export type JobStage = "ANALYZING" | "DIAGNOSING" | "PLANNING" | "RENDERING" | "EVALUATING" | "DECIDING" | "LEARNING" | "DONE" | "FAILED";

export const EXPERIMENT_STAGES: JobStage[] = ["ANALYZING", "DIAGNOSING", "PLANNING", "RENDERING", "EVALUATING", "DECIDING", "LEARNING", "DONE"];

export interface JobEvent {
  seq: number;
  ts: string;
  stage: string;
  message: string;
  data: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------------------------
// Experiments
// ---------------------------------------------------------------------------------------------

export interface ExperimentSummary {
  id: string;
  video_id: string;
  created_at: string;
  policy_mode: string;
  generation: number;
  outcome: string | null;
  winner_label: string | null;
  arms: number;
  policy_version_before: number;
  policy_version_after: number | null;
  total_ms: number | null;
  weave_url: string | null;
  recorded: boolean;
}

export interface FitnessComponent {
  name: string;
  value: number | null;
  unit: string;
  better: "higher" | "lower";
  evidence: string;
  detail: string;
  trials: number | null;
  valid: number | null;
}

export type ArmOutcome = "win" | "loss" | "neutral" | "rejected";

export interface ArmMutation {
  type: string;
  description: string;
  changed_variable: string;
  protected_variables: string[];
  hypothesis_id: string;
  target_start_ms: number;
  target_end_ms: number;
  expected_benefit: string;
  risk: string;
}

export interface Arm {
  id: string;
  label: "control" | "A" | "B" | "C" | string;
  status: string;
  media_url: string | null;
  hook_media_url: string | null;
  duration_ms: number | null;
  render_ms: number | null;
  outcome: ArmOutcome | null;
  outcome_reason: string | null;
  mutation: ArmMutation | null;
  fitness: { hard_gates_passed: boolean; gate_notes: string[]; components: FitnessComponent[] } | null;
  preference_reasons: { full: string[]; hook: string[] };
}

export interface ExperimentDecision {
  outcome: string;
  winner_arm_id: string | null;
  reason: string;
  primary_metric: string;
  per_arm: Record<string, string>;
}

export interface StrategySnapshot {
  status?: string;
  wins?: number;
  losses?: number;
  neutral?: number;
  rejected?: number;
  confidence?: number;
  [key: string]: unknown;
}

export interface PolicyUpdate {
  strategy: string;
  mutation: string;
  outcome: string;
  before: StrategySnapshot;
  after: StrategySnapshot;
  policy_version: number;
}

export interface ReferencePatternUse {
  mutation: string;
  pattern: string;
  description: string;
  count: number;
  total: number;
  top: string | null;
  bottom: string | null;
  label: string;
}

// docs/API_CREATIVE.md writes ExperimentSummary & { arms: Arm[] }, but the summary already has arms: number; the detail
// carries the arm list, so the count is omitted here.
export interface ExperimentDetail extends Omit<ExperimentSummary, "arms"> {
  question: string;
  hypotheses: Hypothesis[];
  ranking: Ranking[];
  arms: Arm[];
  decision: ExperimentDecision | null;
  policy_updates: PolicyUpdate[];
  reference_patterns: ReferencePatternUse[];
  suite: { id: string; hash: string; questions: { id: string; text: string }[] };
  timings_ms: Record<string, number>;
  model_calls: number;
  notes: string[];
}

// ---------------------------------------------------------------------------------------------
// Policy, corpus, transfer, human review
// ---------------------------------------------------------------------------------------------

export type StrategyStatus =
  | "REFERENCE_PRIOR"
  | "PROPOSED"
  | "SUPPORTED_OFFLINE"
  | "HUMAN_SUPPORTED"
  | "REAL_WORLD_SUPPORTED"
  | "CONTRADICTED"
  | "REJECTED";

export interface Strategy {
  id: string;
  mutation: string;
  scope: string;
  status: StrategyStatus;
  wins: number;
  losses: number;
  neutral: number;
  rejected: number;
  human: string | null;
  confidence: number;
  evidence: { experiment_id: string; video_id: string; outcome: string; note: string; weave_url: string | null }[];
}

export interface PolicyChange {
  version: number;
  experiment_id: string;
  strategy_id: string;
  before: StrategySnapshot;
  after: StrategySnapshot;
  created_at: string;
}

export interface PolicyStore {
  version: number;
  strategies: Strategy[];
  changes: PolicyChange[];
}

export interface CorpusPattern {
  pattern_id: string;
  description: string;
  count: number;
  total_comparable: number;
  support_ratio: number;
  top_group_count: number | null;
  top_group_total: number | null;
  bottom_group_count: number | null;
  bottom_group_total: number | null;
  performance_metric: string;
  note: string;
}

export interface Corpus {
  id: string;
  version: number;
  references: number;
  with_metrics: number;
  patterns: CorpusPattern[];
  label: "REFERENCE CREATIVE (owned Curio shorts; descriptive counts)" | string;
}

export interface TransferMode {
  order: string[];
  first_mutation: string | null;
  first_outcome_on_b: string | null;
  attempts_until_first_win: number | null;
  model_calls_until_then: number;
  eval_and_render_ms_until_then: number;
  note: string | null;
}

export interface TransferReport {
  video_id: string;
  policy_version: number;
  oracle_experiment: string;
  outcomes_on_b: Record<string, ArmOutcome>;
  modes: { none: TransferMode; learned: TransferMode };
  first_choice_changed: boolean;
  weave_url: string | null;
  oracle_weave_url: string | null;
  created_at: string;
}

export interface ReviewPair {
  experiment_id: string;
  test_type: "hook" | "full";
  arms: string[];
  arm_labels: Record<string, string>;
  n: number;
  preferred: Record<string, number>;
  no_preference: number;
  label: string;
}

export interface ReviewSummary {
  pairs: ReviewPair[];
  total_responses: number;
}

export interface ReviewQr {
  url: string | null;
  qr_png: string | null;
  note: string;
}

// ---------------------------------------------------------------------------------------------
// Health, uploads and director runs (directorloop/api/app.py, runtime/director.py, audit/models.py)
// ---------------------------------------------------------------------------------------------

export type FeatureName = "runs" | "causal" | "abc" | "url_ingest" | "classify" | "screening";

export interface Health {
  status: string;
  screening?: import("./screening").ScreeningStatus;
  features?: Partial<Record<FeatureName, boolean>>;
  capability_notes?: Partial<Record<FeatureName, string>>;
  limits?: { upload_max_bytes?: number; upload_max_mb?: number };
  weave: { connected: boolean; project: string; traces_url: string | null; reason?: string };
  providers: { name: string; role: string; model: string; state: string }[];
  policy_version: number;
  corpus?: { references: number; with_metrics: number; patterns: number };
  review?: { public_url: string | null; local_url: string };
}

export interface UploadResult {
  video_id: string;
  title: string;
  sha256: string;
  duration_ms: number;
  width: number;
  height: number;
  has_audio: boolean;
  media_url: string;
}

export const OBJECTIVES = ["attention", "comprehension", "emotional_payoff", "sharing", "motivation_to_engage", "other"] as const;
export const EDIT_TYPES = ["REMOVE_BEAT", "MOVE_BEAT_EARLIER", "MOVE_BEAT_LATER", "TRIM_PAUSE", "PUNCH_IN"] as const;

export interface RunRequest {
  video_id: string;
  objective: string;
  constraints: string[];
  focus?: string | null;
  allowed_edits?: string[] | null;
  iteration_budget: number;
  owner_confirms_rights?: boolean;
  idempotency_key?: string;
}

export interface RunStart {
  job_id: string;
  run_id: string;
  created: boolean;
}

export type RunStatus = "running" | "completed" | "failed";
export type IterationDecision = "accept" | "reject_keep_current" | "incomplete_keep_current" | "stop";
export type NextAction = "continue_from_candidate" | "try_alternative" | "stop";

export interface RunSummary {
  id: string;
  video_id: string;
  status: RunStatus;
  created_at: string;
  ended_at: string | null;
  objective: string;
  iteration_budget: number;
  iterations: number;
  decisions: IterationDecision[];
  final_version_id: string;
  final_decision: string;
  stop_reason: string;
  weave_url: string | null;
  launched_via: string;
  total_ms: number | null;
}

export interface RunConfig {
  video_id: string;
  objective: string;
  focus: string | null;
  constraints: string[];
  allowed_edits: string[] | null;
  iteration_budget: number;
  max_attempts_per_finding: number;
  category: string;
}

export interface ConsideredFinding {
  finding_id: string;
  interval_ms: [number, number];
  issue_type: string;
  objective: string;
  severity: string;
  uncertainty: string;
  weakness: string;
  route: string | null;
  runnable: boolean;
  edit: string;
  selection_reason: string;
  why_not_runnable: string | null;
  skipped: string | null;
}

export interface IterationRecord {
  index: number;
  current_version_id: string;
  current_artifact_hash: string;
  audit_id: string;
  considered: ConsideredFinding[];
  finding_id: string | null;
  finding: string;
  ranking_reason: string;
  selection_reason: string;
  repair_run_id: string | null;
  edit: string;
  mutation_type: string | null;
  change_verified: boolean | null;
  verification_checks: string[];
  candidate_version_id: string | null;
  candidate_artifact_hash: string | null;
  candidate_audit_id: string | null;
  candidate_media_url: string | null;
  outcome: string | null;
  target_resolved: string | null;
  improved: string[];
  regressed: string[];
  decision: IterationDecision;
  reason: string;
  next_action: NextAction;
  next_action_reason: string;
  lesson_id: string | null;
  timings_ms: Record<string, number>;
}

export interface RunLesson {
  lesson_id: string;
  run_id: string;
  repair_run_id: string | null;
  video_id: string;
  created_at: string;
  finding?: { issue_type?: string; objective?: string; severity?: string; uncertainty?: string; interval_ms?: [number, number]; weakness?: string };
  repair?: { route?: string; mutation_type?: string; edit?: string; selection_reason?: string };
  result?: { status?: string; outcome?: string; target_resolved?: string; improved?: string[]; regressed?: string[]; incomplete_reasons?: string[] };
  uncertainty?: { stability?: { runs: number; agreeing_runs: number; order_flips: number; agreement: string } | null; change_verified?: boolean | null };
  applies_when?: string;
  evidence_class?: string;
  label?: string;
  [key: string]: unknown;
}

export interface RunDetail {
  id: string;
  config: RunConfig;
  status: RunStatus;
  stop_reason: string;
  final_decision: string;
  created_at: string;
  ended_at: string | null;
  original_artifact_hash: string;
  final_version_id: string;
  final_artifact_hash: string;
  iterations: IterationRecord[];
  audit_ids: string[];
  repair_run_ids: string[];
  lessons: RunLesson[];
  runtime: Record<string, unknown>;
  mocked_stages: string[];
  manual_interventions: string[];
  annotations: string[];
  launched_via: string;
  weave_url: string | null;
  weave_call_id: string | null;
  timings_ms: Record<string, number>;
  error: string | null;
  original_media_url: string;
  final_media_url: string;
}

/** Stages a run emits, in order. The audit's own stages appear inside AUDITING and FRESH_REVIEW. */
export const RUN_JUDGE_STAGES = ["STARTED", "AUDITING", "ANALYZING", "COLD_REVIEW", "DIAGNOSING", "VERIFYING", "AUDIT_DONE"] as const;
export const RUN_ATTEMPT_STAGES = ["SELECTING", "REPAIRING", "VERIFYING", "FRESH_REVIEW", "COMPARING", "DECIDED"] as const;

export type AttentionRisk = "low" | "medium" | "high" | "unknown";
export type Level = "low" | "medium" | "high";
export type Verdict = "YES" | "MAYBE" | "NO" | "INSUFFICIENT_EVIDENCE";
export type RepairRoute = "A" | "B" | "C";

export interface InspectedWindow {
  kind: "prefix" | "diagnostic" | "closeup";
  start_ms: number;
  end_ms: number;
  frame_timestamps_ms: number[];
  frame_width: number;
  context_frames: number;
  context_width: number;
  transcript_words: number;
}

export interface Coverage {
  mode: "sampled_frames_plus_asr" | "native_video" | "transcript_only";
  duration_ms: number;
  windows: InspectedWindow[];
  total_frames_sent: number;
  transcript_source: string;
  audio_inspection: string;
  provider: string;
  model: string;
  limitations: string[];
}

export interface WindowReaction {
  start_ms: number;
  end_ms: number;
  understanding: string;
  expectation: string;
  open_question: string;
  reaction: "engaged" | "neutral" | "losing_interest" | "confused" | "unknown";
  attention_risk: AttentionRisk;
  cause: string;
  label: string;
  latency_ms: number;
  error: string | null;
}

export interface EvidenceFrame {
  t_ms: number;
  url: string | null;
}

export interface Observation {
  kind: "visual" | "audio" | "caption" | "mechanical";
  text: string;
  t_ms: number | null;
}

export interface AuditRepair {
  summary: string;
  route: RepairRoute;
  runnable: boolean;
  mutation_type: string | null;
  edit_description: string;
  keep_unchanged: string[];
  required_materials: string[];
  dependency_state: string;
  secondary_changes: string[];
  why_not_runnable: string | null;
  selection_reason: string;
  implements_proposal: boolean;
  proposal_route: RepairRoute | null;
  proposal_needs: string;
  candidates_offered: string[];
}

export interface AuditFinding {
  id: string;
  version_id: string;
  start_ms: number;
  end_ms: number;
  evidence_frames: EvidenceFrame[];
  observed: Observation[];
  viewer_understanding: string;
  predicted_reaction: string;
  weakness: string;
  issue_type: string;
  alternatives: string[];
  objective: string;
  severity: Level;
  uncertainty: Level;
  proposed_repair: string;
  repair_kind: string;
  keep_unchanged: string[];
  repair: AuditRepair | null;
  verified_closeup: boolean;
  closeup_note: string;
}

export interface AuditStrength {
  id: string;
  start_ms: number;
  end_ms: number;
  what: string;
  why: string;
  evidence_frames: EvidenceFrame[];
}

export interface AudienceVerdict {
  verdict: Verdict;
  reason: string;
  at_ms: number | null;
  to_whom: string | null;
}

export const AUDIENCE_KEYS = ["stop", "continue_watching", "finish", "like", "send", "comment", "save_or_replay", "visit_creator"] as const;
export type AudienceKey = (typeof AUDIENCE_KEYS)[number];

export type AudienceResponses = { label: string } & Record<AudienceKey, AudienceVerdict>;

export interface Audit {
  id: string;
  video_id: string;
  version_id: string;
  artifact_hash: string;
  duration_ms: number;
  created_at: string;
  status: "complete" | "incomplete";
  coverage: Coverage;
  window_reactions: WindowReaction[];
  findings: AuditFinding[];
  strengths: AuditStrength[];
  audience: AudienceResponses;
  overall_summary: string;
  weave_url: string | null;
  weave_call_id: string | null;
  timings_ms: Record<string, number>;
  model_calls: number;
  notes: string[];
  incomplete_reasons: string[];
  primed_with: string[];
  media_url: string | null;
}

export type RepairStatus = "proposed" | "rendered" | "reviewed" | "accepted" | "rejected" | "incomplete";
export type ComparisonOutcome = "improvement" | "regression" | "mixed" | "tie" | "insufficient_evidence";

export interface IntervalMapEntry {
  orig_start_ms: number;
  orig_end_ms: number;
  new_start_ms: number | null;
  new_end_ms: number | null;
  note?: string;
}

export interface RepairComparison {
  improved: string[];
  regressed: string[];
  unchanged: string[];
  target_resolved: "yes" | "no" | "unclear";
  target_evidence: string[];
  new_weaknesses: string[];
  outcome: ComparisonOutcome;
  target_preference: number | null;
  full_preference: number | null;
  stability: { runs: number; agreeing_runs: number; order_flips: number; agreement: string } | null;
  label: string;
  notes: string[];
}

export interface Repair {
  id: string;
  audit_id: string;
  finding_id: string;
  video_id: string;
  status: RepairStatus;
  hypothesis: string;
  repair: AuditRepair;
  original_artifact_hash: string;
  candidate_artifact_hash: string | null;
  candidate_version_id: string | null;
  interval_map: IntervalMapEntry[];
  change_verification: { intended: string; verified: boolean; checks: string[] } | null;
  candidate_audit_id: string | null;
  comparison: RepairComparison | null;
  weave_url: string | null;
  created_at: string;
  timings_ms: Record<string, number>;
  incomplete_reasons: string[];
  lesson_id: string | null;
  candidate_media_url: string | null;
}

// ---------------------------------------------------------------------------------------------
// Judge-only audits, link ingestion, A/B-to-C
// ---------------------------------------------------------------------------------------------

export interface AuditSummary {
  id: string;
  video_id: string;
  version_id: string;
  status: "complete" | "incomplete" | string;
  created_at: string;
  findings: number;
  strengths: number;
  duration_ms: number;
  weave_url: string | null;
}

export const AUDIT_STAGES = ["ANALYZING", "COLD_REVIEW", "DIAGNOSING", "VERIFYING", "AUDIT_DONE"] as const;
export const INGEST_STAGES = ["RESOLVING", "DOWNLOADING", "PROBING", "REGISTERED"] as const;
export const ABC_STAGES = ["STARTED", "PREPARING", "AUDITING", "AUDITS_DONE", "ALIGNING", "COMPARING_AB", "COMPARED_AB", "DIRECTING", "RENDERING_C", "VERIFYING_C", "REVIEWING_C", "JUDGING_C", "DECIDED_C"] as const;

export interface IngestStart {
  job_id: string;
  created: boolean;
  kind: "direct" | "platform";
  platform: string | null;
  edit_permission: string;
}

export interface ABCRequest {
  a_video_id: string;
  b_video_id: string;
  objective: string;
  creative_type?: string;
  audience?: string;
  expected_payoff?: string;
  encounter?: string;
  constraints: string[];
  iteration_budget: number;
  max_model_calls?: number;
  deadline_s?: number;
  owner_confirms_rights?: boolean;
  idempotency_key?: string;
}

export interface ABCStart {
  job_id: string;
  abc_id: string;
  created: boolean;
}

export interface ABCSummary {
  id: string;
  status: RunStatus;
  created_at: string;
  ended_at: string | null;
  objective: string;
  a_video_id: string | null;
  b_video_id: string | null;
  ab_overall: string | null;
  attempts: number;
  decisions: string[];
  final_version: string;
  final_decision: string;
  stop_reason: string;
  weave_url: string | null;
  launched_via: string;
  total_ms: number | null;
  rubric_version: string | null;
}

export interface VersionRef {
  label: string;
  video_id: string;
  title: string;
  role: "input" | "director";
  original_hash: string | null;
  evaluated_hash: string;
  duration_ms: number;
  audit_id: string | null;
  audit_status: string | null;
  genome_cached: boolean | null;
  notes: string[];
  media_url: string | null;
}

export interface AlignedUnit {
  index: number;
  kind: "shared" | "reworded" | "only_a" | "only_b" | "time_aligned";
  a_beats: string[];
  b_beats: string[];
  a_start_ms: number | null;
  a_end_ms: number | null;
  b_start_ms: number | null;
  b_end_ms: number | null;
  text_a: string;
  text_b: string;
  similarity: number | null;
}

export interface DimensionJudgment {
  dimension: string;
  /** A version label, "same", "unstable" (the two presentation orders disagreed) or "unclear". */
  verdict: string;
  votes: Record<string, number>;
  reasons: string[];
}

export interface PairComparison {
  first: string;
  second: string;
  scope: "whole" | "region";
  region: Record<string, unknown> | null;
  dimensions: DimensionJudgment[];
  overall: DimensionJudgment;
  calls: number;
  failed_calls: number;
  rubric_version: string;
  label: string;
}

export interface ABComparison {
  comparable: boolean;
  confounds: string[];
  transcript_similarity: number | null;
  alignment: AlignedUnit[];
  whole: PairComparison | null;
  regions: PairComparison[];
  findings_by_unit: Record<string, string[]>;
  strengths_by_unit: Record<string, string[]>;
  best_supported: string;
  best_supported_reason: string;
}

export interface CProposal {
  option_key: string;
  base: "A" | "B";
  donor: "A" | "B" | null;
  description: string;
  target_dimensions: string[];
  expected_improvement: string;
  protected_strengths: { source: string; item: string }[];
  tradeoffs: string[];
  why_smallest: string;
  selector_reason: string;
  options_offered: string[];
}

export interface CEvaluation {
  candidate_audit_id: string | null;
  candidate_audit_status: string | null;
  change_verification: { intended: string; verified: boolean; checks: string[] } | null;
  vs_base: PairComparison | null;
  vs_other: PairComparison | null;
  target_region: PairComparison | null;
  protected: { item: string; kind?: string; status?: string; evidence?: string; source?: string }[];
  new_weaknesses: string[];
  improved: string[];
  regressed: string[];
  unchanged: string[];
  uncertain: string[];
  outcome: ComparisonOutcome;
  outcome_reason: string;
}

export interface CAttempt {
  index: number;
  proposal: CProposal | null;
  render_hash: string | null;
  version_id: string | null;
  evaluation: CEvaluation | null;
  decision: "accept" | "reject_keep_inputs" | "incomplete" | "stop";
  reason: string;
  next_action: "try_another_c" | "stop";
  next_action_reason: string;
  lesson_id: string | null;
  timings_ms: Record<string, number>;
  render_media_url: string | null;
}

export interface ABCRun {
  id: string;
  status: RunStatus;
  created_at: string;
  ended_at: string | null;
  context: { creative_type: string; audience: string; objective: string; expected_payoff: string; encounter: string; source: string };
  constraints: string[];
  limits: Record<string, unknown>;
  rubric: { version?: string; sha256?: string; whole_dimensions?: Record<string, string>; region_dimensions?: Record<string, string>; verdict_rules?: string; reviewer?: string; selector?: string; [key: string]: unknown };
  versions: Record<string, VersionRef>;
  comparison: ABComparison | null;
  attempts: CAttempt[];
  final_version: string;
  final_decision: string;
  stop_reason: string;
  usage: { model_calls?: number; max_model_calls?: number; input_tokens?: number; output_tokens?: number; cost_usd?: number | null; cost_note?: string; elapsed_s?: number; deadline_s?: number };
  runtime: Record<string, unknown>;
  mocked_stages: string[];
  manual_interventions: string[];
  annotations: string[];
  launched_via: string;
  weave_url: string | null;
  weave_call_id: string | null;
  timings_ms: Record<string, number>;
  error: string | null;
}
