import type { IntervalMapEntry } from "../api/types";

/** Maps an interval of the original onto the candidate timeline using the repair's interval map. */
export function mapInterval(map: IntervalMapEntry[], startMs: number, endMs: number): { start_ms: number; end_ms: number } | null {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const m of map) {
    if (m.new_start_ms === null || m.new_end_ms === null) continue;
    const s = Math.max(startMs, m.orig_start_ms);
    const e = Math.min(endMs, m.orig_end_ms);
    if (e < s || (e === s && startMs !== endMs)) continue;
    const span = Math.max(1, m.orig_end_ms - m.orig_start_ms);
    const scale = (m.new_end_ms - m.new_start_ms) / span;
    lo = Math.min(lo, m.new_start_ms + (s - m.orig_start_ms) * scale);
    hi = Math.max(hi, m.new_start_ms + (e - m.orig_start_ms) * scale);
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  return { start_ms: Math.round(lo), end_ms: Math.round(hi) };
}

/** Greedy lane packing so overlapping intervals never draw on top of each other. */
export function packLanes<T extends { start_ms: number; end_ms: number }>(items: T[]): { item: T; lane: number }[] {
  const sorted = [...items].sort((a, b) => a.start_ms - b.start_ms || b.end_ms - a.end_ms);
  const laneEnds: number[] = [];
  return sorted.map((item) => {
    let lane = laneEnds.findIndex((end) => end <= item.start_ms);
    if (lane < 0) {
      lane = laneEnds.length;
      laneEnds.push(item.end_ms);
    } else {
      laneEnds[lane] = item.end_ms;
    }
    return { item, lane };
  });
}

export function pct(ms: number, durationMs: number): number {
  if (!durationMs) return 0;
  return Math.min(100, Math.max(0, (ms / durationMs) * 100));
}

type MapRow = { orig_start_ms: number; orig_end_ms: number; new_start_ms: number | null; new_end_ms: number | null };

/** Original time to revision time. Outside every mapped interval the offset of the nearest earlier interval applies. */
export function mapTime(map: MapRow[], t: number): number {
  let offset = 0;
  for (const m of [...map].sort((a, b) => a.orig_start_ms - b.orig_start_ms)) {
    if (m.new_start_ms === null || m.new_end_ms === null) continue;
    if (t >= m.orig_start_ms && t <= m.orig_end_ms) {
      const span = Math.max(1, m.orig_end_ms - m.orig_start_ms);
      return m.new_start_ms + ((t - m.orig_start_ms) * (m.new_end_ms - m.new_start_ms)) / span;
    }
    if (m.orig_end_ms < t) offset = m.new_end_ms - m.orig_end_ms;
  }
  return Math.max(0, t + offset);
}

/** Revision time back to original time (inverse of mapTime over mapped intervals). */
export function unmapTime(map: MapRow[], t: number): number {
  let offset = 0;
  for (const m of [...map].sort((a, b) => (a.new_start_ms ?? 0) - (b.new_start_ms ?? 0))) {
    if (m.new_start_ms === null || m.new_end_ms === null) continue;
    if (t >= m.new_start_ms && t <= m.new_end_ms && m.new_end_ms > m.new_start_ms) {
      const span = m.new_end_ms - m.new_start_ms;
      return m.orig_start_ms + ((t - m.new_start_ms) * (m.orig_end_ms - m.orig_start_ms)) / span;
    }
    if (m.new_end_ms < t) offset = m.orig_end_ms - m.new_end_ms;
  }
  return Math.max(0, t + offset);
}
