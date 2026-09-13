import { useRef, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import type { Beat, Genome, WeakRegion } from "../api/types";
import { clamp, fmtRange, fmtS } from "../lib/format";
import { pct } from "../lib/intervals";
import { roleGroup, SOURCE_NAMES } from "../lib/labels";

const SHORT_ROLE: Record<string, string> = {
  hook: "hook",
  setup: "set",
  context: "ctx",
  problem: "prob",
  tension: "tens",
  proof: "proof",
  mechanism: "mech",
  payoff: "pay",
  cta: "cta",
  other: "oth",
};

function tipAlign(centerPct: number): string {
  if (centerPct < 22) return "tip-left";
  if (centerPct > 78) return "tip-right";
  return "tip-center";
}

function Axis({ durationMs, cursorMs, onSeek }: { durationMs: number; cursorMs: number; onSeek: (ms: number) => void }) {
  const step = durationMs > 40000 ? 10000 : durationMs > 16000 ? 5000 : 2000;
  const ticks: number[] = [];
  for (let t = 0; durationMs - t >= step * 0.95; t += step) ticks.push(t);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const delta = e.shiftKey ? 2000 : 250;
    if (e.key === "ArrowRight") onSeek(clamp(cursorMs + delta, 0, durationMs));
    else if (e.key === "ArrowLeft") onSeek(clamp(cursorMs - delta, 0, durationMs));
    else if (e.key === "Home") onSeek(0);
    else if (e.key === "End") onSeek(durationMs);
    else return;
    e.preventDefault();
  };
  return (
    <div
      className="tl-axis"
      role="slider"
      tabIndex={0}
      aria-label="Playhead position"
      aria-valuemin={0}
      aria-valuemax={Math.round(durationMs / 100) / 10}
      aria-valuenow={Math.round(cursorMs / 100) / 10}
      aria-valuetext={fmtS(cursorMs)}
      onKeyDown={onKey}
    >
      {ticks.map((t) => (
        <span key={t} className="tl-tick" style={{ left: `${pct(t, durationMs)}%` }}>
          {(t / 1000).toFixed(0)}s
        </span>
      ))}
      <span className="tl-tick tl-tick-end" style={{ left: "100%" }}>
        {(durationMs / 1000).toFixed(1)}s
      </span>
    </div>
  );
}

function useSeekFromClick(durationMs: number, onSeek: (ms: number) => void) {
  const body = useRef<HTMLDivElement | null>(null);
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    if (!body.current) return;
    const rect = body.current.getBoundingClientRect();
    onSeek(clamp(((e.clientX - rect.left) / rect.width) * durationMs, 0, durationMs));
  };
  return { body, onClick };
}

function Lane({ label, height, children, className }: { label: string; height: number; children: ReactNode; className?: string }) {
  return (
    <div className={`tl-lane ${className ?? ""}`} style={{ height }}>
      <span className="tl-lane-label">{label}</span>
      {children}
    </div>
  );
}

export function BeatBand({ beats, durationMs, onSeek, tall }: { beats: Beat[]; durationMs: number; onSeek: (ms: number) => void; tall?: boolean }) {
  return (
    <>
      {beats.map((b) => {
        const left = pct(b.start_ms, durationMs);
        const width = pct(b.end_ms, durationMs) - left;
        return (
          <button
            key={b.id}
            type="button"
            className={`beat role-${roleGroup(b.role)}${tall ? " beat-tall" : ""}`}
            style={{ left: `${left}%`, width: `${width}%` }}
            onClick={(e) => {
              e.stopPropagation();
              onSeek(b.start_ms);
            }}
            aria-label={`${b.role} beat ${fmtRange(b.start_ms, b.end_ms)}: ${b.text}`}
          >
            <span className="beat-role beat-role-full">{b.role}</span>
            <span className="beat-role beat-role-short" aria-hidden="true">
              {SHORT_ROLE[b.role] ?? b.role.slice(0, 4)}
            </span>
            {tall ? <span className="beat-time mono">{fmtS(b.start_ms)}</span> : null}
            <span className={`tip ${tipAlign(left + width / 2)}`} role="tooltip">
              <span className="tip-head">
                {b.role.toUpperCase()} <span className="mono">{fmtRange(b.start_ms, b.end_ms)}</span>
              </span>
              <span className="tip-text">"{b.text}"</span>
              <span className="tip-meta">
                Role confidence {b.role_confidence.toFixed(2)} · {SOURCE_NAMES.language_model}
                {b.is_question ? " · question" : ""}
                {b.redundant_with_beat_id ? ` · repeats ${b.redundant_with_beat_id}` : ""}
              </span>
            </span>
          </button>
        );
      })}
    </>
  );
}

interface GenomeTimelineProps {
  genome: Genome;
  weakRegion: WeakRegion | null;
  cursorMs: number;
  onSeek: (ms: number) => void;
  target?: { start_ms: number; end_ms: number; label: string } | null;
}

export function GenomeTimeline({ genome, weakRegion, cursorMs, onSeek, target }: GenomeTimelineProps) {
  const d = genome.duration_ms;
  const { body, onClick } = useSeekFromClick(d, onSeek);
  const staticStart = typeof genome.longest_static_span_start_ms.value === "number" ? genome.longest_static_span_start_ms.value : null;
  const staticLen = typeof genome.longest_static_span_ms.value === "number" ? genome.longest_static_span_ms.value : null;
  const cuts = genome.shots.filter((s) => s.start_ms > 0).map((s) => s.start_ms);
  return (
    <div className="tl tl-genome" aria-label="Creative genome timeline">
      <div className="tl-body" ref={body} onClick={onClick}>
        <Lane label="Beats" height={54}>
          <BeatBand beats={genome.beats} durationMs={d} onSeek={onSeek} tall />
          {weakRegion ? (
            <span className="tl-weak" style={{ left: `${pct(weakRegion.start_ms, d)}%`, width: `${pct(weakRegion.end_ms, d) - pct(weakRegion.start_ms, d)}%` }} aria-hidden="true" />
          ) : null}
        </Lane>
        <Lane label="Static" height={12} className="tl-lane-quiet">
          {staticStart !== null && staticLen ? (
            <span
              className="tl-static"
              style={{ left: `${pct(staticStart, d)}%`, width: `${pct(staticStart + staticLen, d) - pct(staticStart, d)}%` }}
              title={`longest static stretch: ${fmtS(staticLen)} from ${fmtS(staticStart)}`}
            />
          ) : null}
        </Lane>
        <Lane label="Cuts" height={20} className="tl-lane-cuts">
          {cuts.length === 0 ? <span className="tl-note">no cuts: one shot</span> : null}
          {cuts.map((t) => (
            <span key={t} className="tl-cut" style={{ left: `${pct(t, d)}%` }} title={`shot cut at ${fmtS(t)}`} />
          ))}
        </Lane>
        {target ? (
          <Lane label="Change" height={16} className="tl-lane-quiet">
            <span className="tl-target" style={{ left: `${pct(target.start_ms, d)}%`, width: `${Math.max(pct(target.end_ms, d) - pct(target.start_ms, d), 1)}%` }} title={target.label} />
          </Lane>
        ) : null}
        <span className="tl-playhead" style={{ left: `${pct(cursorMs, d)}%` }} aria-hidden="true" />
      </div>
      <Axis durationMs={d} cursorMs={cursorMs} onSeek={onSeek} />
      <div className="tl-legend">
        {staticStart !== null && staticLen ? (
          <span className="legend-item">
            <i className="swatch swatch-static" /> Longest static stretch {fmtS(staticLen)} from {fmtS(staticStart)} (mechanical)
          </span>
        ) : null}
        {weakRegion ? (
          <span className="legend-item">
            <i className="swatch swatch-weak" /> Weak region {fmtRange(weakRegion.start_ms, weakRegion.end_ms)}
          </span>
        ) : null}
      </div>
    </div>
  );
}
