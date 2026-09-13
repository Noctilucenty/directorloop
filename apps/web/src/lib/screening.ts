import type { ScreeningReport, ScreeningStatus, ScreenWindow, ScreenObservation } from "../api/screening";
import type { Job } from "../api/types";

export function screeningJobId(screenId: string, requested: string | null): string | null {
  if (!/^screen_[0-9a-f]+_[0-9a-f]+$/.test(screenId)) return null;
  const expected = `job_${screenId.slice("screen_".length)}`;
  return requested === null || requested === expected ? expected : null;
}

export function screeningJobMatches(screenId: string, job: Pick<Job, "id" | "kind" | "params"> | null, videoId?: string): boolean {
  return Boolean(job && job.kind === "screening" && screeningJobId(screenId, job.id) === job.id
    && (!videoId || job.params.video_id === videoId));
}

export function screeningCitation(window: ScreenWindow | undefined, fact: ScreenObservation | undefined) {
  const supplied = new Set(window?.evidence_frames.map(frame => frame.t_ms) ?? []);
  const timestamps = [...new Set(fact?.frame_timestamps_ms ?? [])];
  const normalize = (text: string) => text.trim().replace(/\s+/g, " ");
  const quote = fact?.asr_quote == null ? null : normalize(fact.asr_quote);
  return {
    validTimes: timestamps.filter(time => Number.isInteger(time) && supplied.has(time)),
    invalidTimes: timestamps.filter(time => !Number.isInteger(time) || !supplied.has(time)),
    quoteValid: quote === null ? null : Boolean(quote && normalize(window?.prefix_asr_text ?? "").includes(quote)),
  };
}

export function screeningTitle(report: ScreeningReport | null): string {
  if (!report || report.status === "running") return "Watching three moments";
  if (report.status === "failed") return "Screening stopped";
  if (report.status === "canceled") return "Screening canceled";
  return "Ready for your review";
}

export function screeningRisk(window: ScreenWindow): string {
  if (window.status === "needs_review" || window.validation_issues.length) return "Check evidence";
  if (!window.judgment) return "Not reviewed";
  if (window.attention_context === "last_endcard" || window.attention_context === "last_signoff") return "Video ending";
  return window.judgment.attention_risk === "unknown" ? "Uncertain" : `${window.judgment.attention_risk} risk`;
}

export function screeningBudget(status?: ScreeningStatus): string {
  if (!status?.budget) return "Budget unavailable";
  const budget = status.budget;
  if (!status.available) return "Screening paused";
  return `$${budget.available_usd.toFixed(2)} left in screening budget`;
}
