export function fmtS(ms: number | null | undefined, digits = 1): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "n/a";
  return `${(ms / 1000).toFixed(digits)} s`;
}

export function fmtRange(startMs: number, endMs: number): string {
  return `${(startMs / 1000).toFixed(1)}–${(endMs / 1000).toFixed(1)} s`;
}

export function fmtElapsed(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "n/a";
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms - m * 60000) / 1000);
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

export function fmtShare(v: number | null | undefined): string {
  if (v === null || v === undefined) return "n/a";
  return v.toFixed(2);
}

export function fmtPct(fraction: number | null | undefined, digits = 0): string {
  if (fraction === null || fraction === undefined) return "n/a";
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "";
  const tail = id.split("_").pop() ?? id;
  return tail.slice(0, n);
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "Unknown error";
}

export function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** Formats an ISO timestamp, or the compact YYYYMMDD-HHMMSS stamp some reports use (local time of the machine that wrote it). */
export function fmtStamp(value: string | null | undefined): string {
  if (!value) return "";
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(value);
  if (!m) return fmtDateTime(value);
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]));
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
