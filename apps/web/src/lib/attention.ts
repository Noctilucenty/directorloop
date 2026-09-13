import type { WindowReaction } from "../api/types";

export type Risk = "low" | "medium" | "high";

/** Ordinal position of a predicted risk label, used only for ordering and bar height. Not a probability. */
export const RISK_ORDER: Record<Risk, number> = { low: 0, medium: 1, high: 2 };

export const RISK_WORDS: Record<string, string> = { low: "Low", medium: "Medium", high: "High", unknown: "Not reviewed" };

export const REACTION_WORDS: Record<string, string> = {
  engaged: "engaged",
  neutral: "neutral",
  losing_interest: "losing interest",
  confused: "confused",
  unknown: "no reading",
};

export function isRisk(value: string): value is Risk {
  return value === "low" || value === "medium" || value === "high";
}

/** A window counts as reviewed when the review returned a usable risk label and no error. */
export function reviewed(w: WindowReaction): boolean {
  return !w.error && isRisk(w.attention_risk);
}

export interface AttentionStory {
  windows: WindowReaction[];
  reviewedCount: number;
  maxRisk: Risk | null;
  /** Index of the first reviewed window above low. */
  firstRise: number | null;
  /** Inclusive index range of the stretch that starts at firstRise and stays above low. */
  firstStretch: [number, number] | null;
  /** Inclusive index ranges of consecutive reviewed windows at the highest risk seen. */
  peakRuns: [number, number][];
  /** First reviewed low window after the first elevated stretch. */
  recovery: number | null;
  /** The last reviewed window is above low. */
  endsElevated: boolean;
}

/**
 * Reads the per-window predictions exactly at the granularity the review returned: which window first rises above low,
 * where the highest predicted risk sits, and whether a later window returns to low. Windows without a reading break runs.
 */
export function readAttention(input: WindowReaction[]): AttentionStory {
  const windows = [...input].sort((a, b) => a.start_ms - b.start_ms);
  const levels = windows.map((w) => (reviewed(w) ? RISK_ORDER[w.attention_risk as Risk] : null));
  const known = levels.filter((l): l is number => l !== null);
  const story: AttentionStory = { windows, reviewedCount: known.length, maxRisk: null, firstRise: null, firstStretch: null, peakRuns: [], recovery: null, endsElevated: false };
  if (!known.length) return story;
  const max = Math.max(...known);
  story.maxRisk = (Object.keys(RISK_ORDER) as Risk[]).find((r) => RISK_ORDER[r] === max) ?? null;
  const lastKnown = levels.map((l, i) => (l === null ? -1 : i)).filter((i) => i >= 0).pop() as number;
  story.endsElevated = (levels[lastKnown] ?? 0) > 0;
  if (max === 0) return story;

  story.firstRise = levels.findIndex((l) => l !== null && l > 0);
  let end = story.firstRise;
  while (end + 1 < levels.length && (levels[end + 1] ?? 0) > 0 && levels[end + 1] !== null) end += 1;
  story.firstStretch = [story.firstRise, end];
  for (let i = end + 1; i < levels.length; i += 1) {
    if (levels[i] === 0) {
      story.recovery = i;
      break;
    }
  }
  for (let i = 0; i < levels.length; i += 1) {
    if (levels[i] !== max) continue;
    const last = story.peakRuns[story.peakRuns.length - 1];
    if (last && last[1] === i - 1) last[1] = i;
    else story.peakRuns.push([i, i]);
  }
  return story;
}
