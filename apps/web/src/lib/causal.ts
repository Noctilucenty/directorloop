import type { CausalArm, CausalPolicyEffect, CausalRun, CausalSummary } from "../api/causal";
import type { JobEvent } from "../api/types";

export function incompleteComparison(arm: CausalArm): boolean {
  return arm.evaluation_complete === false || arm.comparison?.outcome === "insufficient_evidence";
}

export function causalFailureMessage(error: string): string {
  if (creditsExhausted(error)) return "Add credits to the configured provider account, then start a new session.";
  return error;
}

const creditsExhausted = (error: string) => /credit_balance_exhausted|insufficient[_ ]quota|provider credits exhausted|no credits remaining/i.test(error);
const genericFailure = (error: string) => {
  const cause = error.trim().replace(/^the run stopped with an error\b\s*(?::\s*)?/i, "").trim();
  return !cause || /^[.!]$/.test(cause) || /operation failed;\s*inspect saved job stages|^\s*(?:\w+(?:Error|Exception):\s*)?(?:unknown error|operation failed)\s*$/i.test(cause);
};

/** Only failure evidence from this session is admitted. A worker wrapper cannot hide its saved cause. */
export function causalFailure({ runError, jobError, stopReason, runFailed, events = [] }: {
  runError?: string | null; jobError?: string | null; stopReason?: string | null; runFailed?: boolean; events?: JobEvent[];
}): { title: string; message: string; details: Record<string, string> } | null {
  const terminalEvent = [...events].reverse().find((event) => event.stage === "FAILED");
  const eventError = typeof terminalEvent?.data?.error === "string" ? terminalEvent.data.error : terminalEvent?.message;
  const entries = [
    ["Run error", runError], ["Job error", jobError],
    ["Stop reason", runFailed ? stopReason : null], ["Terminal failure event", eventError],
  ].filter((entry): entry is [string, string] => typeof entry[1] === "string" && Boolean(entry[1].trim()));
  if (!entries.length) return null;
  const selected = entries.find(([, value]) => !genericFailure(value))?.[1] ?? entries[0][1];
  return {
    title: creditsExhausted(selected) ? "Credits exhausted" : "Session stopped",
    message: creditsExhausted(selected) ? causalFailureMessage(selected) : "Open the saved failure details before starting another session.",
    details: Object.fromEntries(entries),
  };
}

export function causalJobLabel(state?: string, stage?: string): string {
  const readable = (value: string) => value.toLowerCase().replaceAll("_", " ");
  if (!state) return "Connecting";
  const parts = [readable(state), ...(stage && readable(stage) !== readable(state) ? [readable(stage)] : [])];
  return parts.map((part) => part[0].toUpperCase() + part.slice(1)).join(" · ");
}

export function causalOutcome(run: CausalSummary | CausalRun): string {
  if (run.status === "running") return "In progress";
  if (run.status === "failed") return "Stopped before completion";
  if (run.status === "cancelled") return "Canceled";
  const planOnly = "config" in run ? run.config.plan_only : run.plan_only;
  const decision = "plan" in run ? run.plan?.decision : run.decision;
  if (decision === "do_not_edit") return "No defensible edit";
  if (planOnly) return "Plan only · no edit tested";
  const verdicts = "config" in run ? run.arms.map((arm) => arm.verdict) : run.verdicts;
  if ("config" in run && run.arms.some(incompleteComparison)) return run.arms.some((arm) => arm.verdict === "win" && !incompleteComparison(arm)) ? "A revision won; another comparison is incomplete" : "A comparison has insufficient evidence";
  if (verdicts.includes("win")) return "A revision won the model comparison";
  if (verdicts.some((v) => v === "incomplete" || v === "inconclusive")) return "Evidence is incomplete or inconclusive";
  if (verdicts.length) return "No tested revision won";
  return "No comparison recorded";
}

export function policyEffectLabel(effect: CausalPolicyEffect): string {
  if (effect.first_choice_changed) return "Memory changed the first experiment";
  if (effect.selection_changed) return "Memory changed the selected experiments";
  if (effect.order_changed) return "Memory changed the ranking";
  if (effect.score_changes.length) return "Scores changed; the choice did not";
  return "Memory did not change the choice";
}

export function recordedValue(value: unknown): string {
  if (value === null || value === undefined) return "Not recorded";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value || "Not recorded";
  if (Array.isArray(value)) return value.length ? value.map(recordedValue).join(" · ") : "None recorded";
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key.replaceAll("_", " ")}: ${recordedValue(item)}`).join("; ");
  return String(value);
}

export function causalCost(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(4)}` : "Unknown";
}

export function mediaFailure(status: number): { title: string; detail: string } {
  if (status === 409) return { title: "Source file changed", detail: "These bytes no longer match the recorded video hash. Playback is blocked to preserve the evidence." };
  if (status === 404) return { title: "Media file missing", detail: "This recorded video is no longer available on this server. The result remains inspectable." };
  return { title: "Video not available here", detail: "The media file could not be loaded." };
}
