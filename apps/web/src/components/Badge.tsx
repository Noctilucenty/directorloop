export type BadgeKind =
  | "live"
  | "cached"
  | "recorded"
  | "probe"
  | "mechanical"
  | "human"
  | "unverified"
  | "fresh"
  | "neutral"
  | "ok"
  | "bad"
  | "warn";

const LABELS: Record<BadgeKind, string> = {
  live: "LIVE",
  cached: "CACHED",
  recorded: "RECORDED RUN",
  probe: "MODEL PROBE",
  mechanical: "MECHANICAL CHECK",
  human: "HUMAN REVIEW",
  unverified: "UNVERIFIED",
  fresh: "FRESH",
  neutral: "",
  ok: "",
  bad: "",
  warn: "",
};

export function Badge({ kind, label, large }: { kind: BadgeKind; label?: string; large?: boolean }) {
  const text = label ?? LABELS[kind];
  return <span className={`badge badge-${kind}${large ? " badge-large" : ""}`}>{text}</span>;
}

export function modeBadge(mode: "fresh" | "cached" | "recorded" | string): BadgeKind {
  if (mode === "cached") return "cached";
  if (mode === "recorded") return "recorded";
  if (mode === "fresh") return "fresh";
  return "neutral";
}
