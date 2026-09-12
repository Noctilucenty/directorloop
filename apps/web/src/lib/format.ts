export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "n/a";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}m ${rest.toFixed(0)}s`;
}

export function fmtSeconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "n/a";
  return `${(ms / 1000).toFixed(1)} s`;
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function fmtPct(fraction: number | null | undefined, digits = 0): string {
  if (fraction === null || fraction === undefined) return "n/a";
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function fmtUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "unknown";
  return `$${value.toFixed(value < 0.01 && value > 0 ? 4 : 2)}`;
}

export function shortHash(hash: string | null | undefined, n = 10): string {
  if (!hash) return "";
  return hash.slice(0, n);
}

export function humanize(id: string): string {
  return id.replace(/^q_/, "").replace(/_/g, " ");
}

export function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
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
