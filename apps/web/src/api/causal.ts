import type { WorkflowStage } from "../lib/workflow";

/** The causal API is distinct from the older single-edit /runs API. */
export interface CausalRequest {
  video_id: string;
  objective?: string;
  constraints?: string[];
  arms: number;
  plan_only: boolean;
  max_model_calls: number;
  deadline_s: number;
  idempotency_key: string;
  owner_confirms_rights?: boolean;
  audit_id?: string;
}
export interface CausalStart { job_id: string; causal_id: string; created: boolean }
export type CausalVerdict = "win" | "neutral" | "loss" | "rejected" | "inconclusive" | "incomplete";
export interface CausalSummary {
  id: string; video_id: string; status: "running" | "completed" | "failed" | "cancelled";
  created_at: string; ended_at: string | null; objective: string; plan_only: boolean;
  arms: number; audit_id: string | null; audit_source: "fresh" | "recorded" | null;
  decision: string | null; verdicts: CausalVerdict[]; stop_reason: string;
  weave_url: string | null; launched_via: string; total_ms: number | null;
}
export interface CausalEvidence {
  id: string; source: string; text: string; start_ms: number | null; end_ms: number | null;
  supports: string[]; against: string[];
}
export interface CausalHypothesis {
  id: string; cause_type: string; statement: string; status: string; strength: string;
  evidence_for: string[]; evidence_against: string[]; why_not_high: string[];
  testable: boolean; testable_edits: string[];
}
export interface CausalExperiment {
  id: string; hypothesis_id: string; cause_type: string; also_tests: string[];
  intervention_id: string; edit_key: string; plan_hash: string; mutation_type: string;
  description: string; changes: string[]; unchanged: string[]; side_effects: string[];
  isolation: string; discriminating: boolean; information_gain: string;
  evidence_strength: string; prior: { score: number; records: number; matches: Record<string, unknown>[]; excluded: Record<string, unknown>[] };
  priority: number; priority_without_policy: number; rank: number; rank_without_policy: number;
}
export interface CausalPolicyEffect {
  records_used: string[]; score_changes: string[]; rank_changes: string[];
  order_changed: boolean; selection_changed: boolean; first_choice_changed: boolean; summary: string;
}
export interface CausalArm {
  experiment_id: string; hypothesis_id: string; cause_type: string; mutation_type: string;
  description: string; changes: string[]; unchanged: string[]; side_effects: string[];
  render_media_url: string | null; render_hash: string | null; duration_ms: number | null;
  verified: boolean | null; checks: string[]; candidate_audit_id: string | null;
  review_inputs: Record<string, string>; evaluator_differences: string[];
  protected_items: string[]; axes: Record<string, unknown> | null;
  comparison: Record<string, unknown> | null; verdict: CausalVerdict; verdict_reason: string;
  evaluation_complete?: boolean | null; policy_eligible?: boolean | null;
  learning_blockers?: string[]; policy_record_id: string | null;
}
export interface CausalRun {
  id: string; config: Omit<CausalRequest, "idempotency_key"> & { category: string };
  version: string; status: CausalSummary["status"]; created_at: string; ended_at: string | null;
  audit_id: string | null; audit_source: CausalSummary["audit_source"]; artifact_hash: string;
  original_media_url: string | null;
  dossier: null | {
    symptom: { kind: string; start_ms: number; end_ms: number; severity: string; description: string };
    evidence: CausalEvidence[];
    open_loops: { established_ms: number | null; expectation: string; resolved_ms: number | null; risk_rises_at_ms: number | null; delay_to_payoff_ms: number | null }[];
  };
  plan: null | {
    hypotheses: CausalHypothesis[]; experiments: CausalExperiment[];
    decision: string; decision_reason: string; chosen: string[]; chosen_without_policy: string[];
    policy_effect: CausalPolicyEffect; uncertainty: string; best_discriminating_test: string;
    formulas: Record<string, string>;
  };
  arms: CausalArm[]; conclusions: string[]; hypothesis_results: Record<string, string>;
  policy_before: string[]; policy_after: string[]; policy_records_added: string[];
  budget: Record<string, unknown>; runtime: Record<string, unknown>; mocked_stages: string[];
  stop_reason: string; error: string | null; launched_via: string; weave_url: string | null;
  timings_ms: Record<string, number>; workflow_stages: WorkflowStage[];
}
