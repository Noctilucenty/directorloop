export interface ScreeningBudget {
  limit_usd: number;
  held_usd: number;
  available_usd: number;
  physical_attempts: number;
  max_physical_attempts: number | null;
  usage_estimate_usd: number;
}

export interface ScreeningStatus {
  enabled: boolean;
  available: boolean;
  reason: string;
  budget: ScreeningBudget | null;
  model: string;
  no_automatic_edit: true;
  review_required: true;
}

export interface ScreeningStart { job_id: string; screen_id: string; created: boolean }
export interface ScreenObservation {
  text: string;
  kind: "visible_fact" | "caption_claim" | "asr_claim" | "inference" | "unknown";
  frame_timestamps_ms: number[];
  asr_quote: string | null;
}
export interface ScreenWindow {
  start_ms: number;
  end_ms: number;
  status: "pending" | "complete" | "needs_review" | "failed" | "not_attempted";
  judgment: null | {
    understanding: string;
    observations: ScreenObservation[];
    attention_risk: "low" | "medium" | "high" | "unknown";
    cause_observation_indices: number[];
    suggestion: string;
    moment_kind: string;
    uncertainties: string[];
  };
  attention_context: "content" | "last_endcard" | "last_signoff" | "unknown";
  validation_issues: string[];
  evidence_frames: { t_ms: number; width: number; height: number; media_url?: string }[];
  prefix_asr_text: string;
  weave_url: string | null;
}
export interface ScreeningReport {
  id: string;
  video_id: string;
  status: "running" | "complete" | "needs_review" | "failed" | "canceled";
  duration_ms: number;
  created_at: string;
  ended_at: string | null;
  error: string | null;
  review_required: true;
  protocol_fingerprint: string;
  windows: ScreenWindow[];
  model_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  weave_url: string | null;
  media_url: string | null;
}
export type ScreeningSummary = Pick<ScreeningReport, "id" | "video_id" | "status" | "created_at" | "ended_at" | "duration_ms" | "model_calls" | "weave_url">;
