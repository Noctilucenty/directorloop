import type { ArmOutcome, ComparisonOutcome, Level, RepairRoute, StrategyStatus, Verdict } from "../api/types";

type GlyphShape = "dot" | "ring" | "bar" | "cross" | "half" | "up" | "down" | "dash";

/** Small shape that pairs with a colored label so state is never carried by color alone. */
export function Glyph({ shape }: { shape: GlyphShape }) {
  const common = { width: 12, height: 12, viewBox: "0 0 12 12", "aria-hidden": true, className: "glyph" } as const;
  switch (shape) {
    case "dot":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="currentColor" />
        </svg>
      );
    case "ring":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
      );
    case "half":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" strokeWidth="2" />
          <path d="M6 2 A4 4 0 0 1 6 10 Z" fill="currentColor" />
        </svg>
      );
    case "bar":
      return (
        <svg {...common}>
          <rect x="1.5" y="4.5" width="9" height="3" rx="1" fill="currentColor" />
        </svg>
      );
    case "cross":
      return (
        <svg {...common}>
          <path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
        </svg>
      );
    case "up":
      return (
        <svg {...common}>
          <path d="M6 2 L10.5 9 H1.5 Z" fill="currentColor" />
        </svg>
      );
    case "down":
      return (
        <svg {...common}>
          <path d="M6 10 L10.5 3 H1.5 Z" fill="currentColor" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <rect x="1.5" y="5" width="9" height="2" rx="1" fill="currentColor" opacity="0.7" />
        </svg>
      );
  }
}

const EVIDENCE_LABELS: Record<string, string> = {
  mechanical: "MECHANICAL",
  model_eval: "MODEL EVAL",
  reference: "REFERENCE",
  historical: "HISTORICAL",
  human_test: "HUMAN TEST",
  real: "REAL",
  simulated: "SIMULATED",
};

export function EvidenceBadge({ kind, suffix }: { kind: string; suffix?: string }) {
  const key = EVIDENCE_LABELS[kind] ? kind : "other";
  const label = EVIDENCE_LABELS[kind] ?? kind.replace(/_/g, " ").toUpperCase();
  return (
    <span className={`badge ev ev-${key}`}>
      {label}
      {suffix ? <span className="badge-suffix">{suffix}</span> : null}
    </span>
  );
}

const OUTCOME: Record<ArmOutcome, { label: string; shape: GlyphShape }> = {
  win: { label: "WIN", shape: "up" },
  loss: { label: "LOSS", shape: "down" },
  neutral: { label: "NEUTRAL", shape: "ring" },
  rejected: { label: "REJECTED", shape: "cross" },
};

export function OutcomePill({ outcome, large }: { outcome: string | null; large?: boolean }) {
  if (!outcome) return <span className={`pill pill-none${large ? " pill-lg" : ""}`}>NO OUTCOME</span>;
  const o = OUTCOME[outcome as ArmOutcome];
  if (!o) return <span className={`pill pill-none${large ? " pill-lg" : ""}`}>{outcome.toUpperCase()}</span>;
  return (
    <span className={`pill out-${outcome}${large ? " pill-lg" : ""}`}>
      <Glyph shape={o.shape} />
      {o.label}
    </span>
  );
}

const VERDICT: Record<Verdict, { label: string; shape: GlyphShape }> = {
  YES: { label: "YES", shape: "dot" },
  MAYBE: { label: "MAYBE", shape: "half" },
  NO: { label: "NO", shape: "cross" },
  INSUFFICIENT_EVIDENCE: { label: "NOT ENOUGH EVIDENCE", shape: "dash" },
};

export function VerdictPill({ verdict }: { verdict: Verdict }) {
  const v = VERDICT[verdict] ?? VERDICT.INSUFFICIENT_EVIDENCE;
  return (
    <span className={`pill verdict verdict-${verdict.toLowerCase()}`}>
      <Glyph shape={v.shape} />
      {v.label}
    </span>
  );
}

const STRATEGY: Record<StrategyStatus, { label: string; tone: string; shape: GlyphShape }> = {
  REFERENCE_PRIOR: { label: "REFERENCE PRIOR", tone: "reference", shape: "ring" },
  PROPOSED: { label: "PROPOSED", tone: "neutral", shape: "ring" },
  SUPPORTED_OFFLINE: { label: "SUPPORTED OFFLINE", tone: "good", shape: "half" },
  HUMAN_SUPPORTED: { label: "HUMAN SUPPORTED", tone: "human", shape: "dot" },
  REAL_WORLD_SUPPORTED: { label: "REAL-WORLD SUPPORTED", tone: "real", shape: "dot" },
  CONTRADICTED: { label: "CONTRADICTED", tone: "warn", shape: "down" },
  REJECTED: { label: "REJECTED", tone: "bad", shape: "cross" },
};

export function StrategyStatusPill({ status }: { status: string }) {
  const s = STRATEGY[status as StrategyStatus] ?? { label: status.replace(/_/g, " "), tone: "neutral", shape: "ring" as GlyphShape };
  return (
    <span className={`pill tone-${s.tone}`}>
      <Glyph shape={s.shape} />
      {s.label}
    </span>
  );
}

export function LevelTag({ name, level }: { name: string; level: Level }) {
  return (
    <span className={`level level-${level}`}>
      <span className="level-name">{name}</span>
      <span className="level-bars" aria-hidden="true">
        <i className="on" />
        <i className={level !== "low" ? "on" : ""} />
        <i className={level === "high" ? "on" : ""} />
      </span>
      <span className="level-value">{level}</span>
    </span>
  );
}

const ROUTES: Record<RepairRoute, string> = {
  A: "Edit the existing video",
  B: "Needs the source project",
  C: "Needs a new asset",
};

export function RouteBadge({ route }: { route: RepairRoute }) {
  return (
    <span className={`route route-${route}`}>
      <span className="route-letter">{route}</span>
      <span className="route-text">{ROUTES[route] ?? "Unknown route"}</span>
    </span>
  );
}

export type RunKind = "recorded" | "live" | "replay";

export function RunBadge({ kind }: { kind: RunKind }) {
  const label = kind === "recorded" ? "RECORDED RUN" : kind === "live" ? "LIVE" : "MOCK REPLAY";
  return <span className={`runbadge run-${kind}`}>{label}</span>;
}

const STATE_TONE: Record<string, { tone: string; shape: GlyphShape }> = {
  proposed: { tone: "neutral", shape: "ring" },
  rendered: { tone: "info", shape: "half" },
  reviewed: { tone: "info", shape: "dot" },
  accepted: { tone: "good", shape: "up" },
  rejected: { tone: "bad", shape: "cross" },
  incomplete: { tone: "warn", shape: "dash" },
  sample: { tone: "sample", shape: "dash" },
  completed: { tone: "info", shape: "dot" },
  running: { tone: "accent", shape: "half" },
  queued: { tone: "neutral", shape: "ring" },
  failed: { tone: "bad", shape: "cross" },
  canceled: { tone: "warn", shape: "bar" },
};

export function StatePill({ state }: { state: string }) {
  const key = state.toLowerCase();
  const s = STATE_TONE[key] ?? { tone: "neutral", shape: "ring" as GlyphShape };
  return (
    <span className={`pill tone-${s.tone}`}>
      <Glyph shape={s.shape} />
      {key.replace(/_/g, " ").toUpperCase()}
    </span>
  );
}

const COMPARISON: Record<ComparisonOutcome, { label: string; tone: string; shape: GlyphShape }> = {
  improvement: { label: "IMPROVEMENT", tone: "good", shape: "up" },
  regression: { label: "REGRESSION", tone: "bad", shape: "down" },
  mixed: { label: "MIXED", tone: "warn", shape: "half" },
  tie: { label: "TIE", tone: "neutral", shape: "ring" },
  insufficient_evidence: { label: "INSUFFICIENT EVIDENCE", tone: "neutral", shape: "dash" },
};

export function ComparisonOutcomePill({ outcome, large }: { outcome: ComparisonOutcome; large?: boolean }) {
  const c = COMPARISON[outcome] ?? { label: String(outcome).toUpperCase(), tone: "neutral", shape: "ring" as GlyphShape };
  return (
    <span className={`pill tone-${c.tone}${large ? " pill-lg" : ""}`}>
      <Glyph shape={c.shape} />
      {c.label}
    </span>
  );
}
