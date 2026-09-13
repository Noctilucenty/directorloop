import { useEffect, useRef, useState, type ReactNode } from "react";
import type { IterationRecord, Repair, RunDetail } from "../api/types";
import { fmtRange, fmtS } from "../lib/format";
import { mapTime, unmapTime } from "../lib/intervals";
import { AUDIENCE_NAMES, OUTCOME_NAMES } from "../lib/labels";
import { LinkedIcon, PauseIcon, PlayIcon, SoundIcon } from "./Icons";
import { ModelJudgment } from "./Judge";
import { Frame } from "./Shell";
import { VideoPlayer, type PlayerHandle } from "./VideoPlayer";

type MapRow = { orig_start_ms: number; orig_end_ms: number; new_start_ms: number | null; new_end_ms: number | null };

export function LinkedPlayers({
  originalSrc,
  revisionSrc,
  revisionLabel,
  map,
  target,
}: {
  originalSrc: string;
  revisionSrc: string;
  revisionLabel: string;
  map: MapRow[];
  target: [number, number] | null;
}) {
  const a = useRef<PlayerHandle>(null);
  const b = useRef<PlayerHandle>(null);
  const [linked, setLinked] = useState(true);
  const [sound, setSound] = useState<"original" | "revision">("original");
  const [ready, setReady] = useState(0);

  useEffect(() => {
    const t = window.setTimeout(() => setReady((r) => r + 1), 50);
    return () => window.clearTimeout(t);
  }, [originalSrc, revisionSrc]);

  useEffect(() => {
    if (!linked) return;
    const va = a.current?.element();
    const vb = b.current?.element();
    if (!va || !vb) return;
    const near = (x: number, y: number) => Math.abs(x - y) < 0.08;
    const follow = (from: HTMLVideoElement, to: HTMLVideoElement, convert: (ms: number) => number) => ({
      play: () => {
        const t = convert(from.currentTime * 1000) / 1000;
        if (!near(to.currentTime, t)) to.currentTime = t;
        if (to.paused) void to.play().catch(() => undefined);
      },
      pause: () => {
        if (!to.paused) to.pause();
      },
      seeked: () => {
        const t = convert(from.currentTime * 1000) / 1000;
        if (!near(to.currentTime, t)) to.currentTime = t;
      },
    });
    const ab = follow(va, vb, (ms) => mapTime(map, ms));
    const ba = follow(vb, va, (ms) => unmapTime(map, ms));
    const pairs: [HTMLVideoElement, string, () => void][] = [
      [va, "play", ab.play],
      [va, "pause", ab.pause],
      [va, "seeked", ab.seeked],
      [vb, "play", ba.play],
      [vb, "pause", ba.pause],
      [vb, "seeked", ba.seeked],
    ];
    pairs.forEach(([el, ev, fn]) => el.addEventListener(ev, fn));
    return () => pairs.forEach(([el, ev, fn]) => el.removeEventListener(ev, fn));
  }, [linked, map, ready]);

  const playTarget = () => {
    if (!target) return;
    a.current?.seek(target[0]);
    b.current?.seek(mapTime(map, target[0]));
    a.current?.play();
    if (!linked) b.current?.play();
  };

  return (
    <div className="linked">
      <div className="linked-players">
        <VideoPlayer ref={a} src={originalSrc} label={<Frame>Original</Frame>} muted={sound !== "original"} />
        <VideoPlayer ref={b} src={revisionSrc} label={<Frame tone="accent" title={revisionLabel}>{revisionLabel}</Frame>} muted={sound !== "revision"} />
      </div>
      <div className="linked-controls" role="group" aria-label="Playback">
        <button type="button" className="btn btn-small" onClick={() => a.current?.play()}>
          <PlayIcon size={14} /> Play
        </button>
        <button type="button" className="btn btn-small btn-quiet" onClick={() => a.current?.pause()}>
          <PauseIcon size={14} /> Pause
        </button>
        {target ? (
          <button type="button" className="btn btn-small btn-quiet" onClick={playTarget}>
            Target moment <span className="mono">{fmtRange(target[0], target[1])}</span>
          </button>
        ) : null}
        <button type="button" className={`btn btn-small btn-quiet${linked ? " is-on" : ""}`} aria-pressed={linked} onClick={() => setLinked((l) => !l)}>
          <LinkedIcon size={14} /> {linked ? "Linked" : "Unlinked"}
        </button>
        <span className="seg" role="radiogroup" aria-label="Sound from">
          <SoundIcon size={14} />
          {(["original", "revision"] as const).map((s) => (
            <button key={s} type="button" role="radio" aria-checked={sound === s} className={`seg-btn${sound === s ? " is-on" : ""}`} onClick={() => setSound(s)}>
              {s === "original" ? "Original" : "Revision"}
            </button>
          ))}
        </span>
      </div>
    </div>
  );
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {rows.map(([k, v]) => (
        <div key={k} className="fact">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

const VERDICT_TEXT: Record<string, string> = { YES: "yes", MAYBE: "maybe", NO: "no", INSUFFICIENT_EVIDENCE: "not enough evidence" };

/** Recorded comparison lines name audience predictions by key ("predicted send: MAYBE -> YES"); show them in words. */
export function readableComparisonLine(line: string): string {
  const m = /^(predicted )?([a-z_]+): ([\s\S]*)$/.exec(line);
  if (!m || !(m[2] in AUDIENCE_NAMES)) return line;
  const same = /^(\S+)\s*->\s*\1\b([\s\S]*)$/.exec(m[3]);
  const rest = (same ? `${same[1]}${same[2]}` : m[3]).replace(/\b(YES|MAYBE|NO|INSUFFICIENT_EVIDENCE)\b/g, (v) => VERDICT_TEXT[v] ?? v).replace(/\s*->\s*/g, " to ");
  return `${AUDIENCE_NAMES[m[2]]}${m[1] ? " (predicted)" : ""}: ${rest}`;
}

function ListBlock({ title, items, empty, tone }: { title: string; items: string[]; empty: string; tone: string }) {
  return (
    <div className={`cmp-block tone-${tone}`}>
      <div className="cmp-title">
        {title}
        <span className="mono cmp-count">{items.length}</span>
      </div>
      {items.length ? (
        <ul className="plain-list">
          {items.map((t) => (
            <li key={t}>{readableComparisonLine(t)}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">{empty}</p>
      )}
    </div>
  );
}

export function RevisionVerdict({ it, repair }: { it: IterationRecord; repair: Repair | null }) {
  const kept = it.decision === "accept";
  const c = repair?.comparison ?? null;
  const pref = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : v.toFixed(2));
  return (
    <section className={`verdict-card ${kept ? "tone-good" : "tone-bad"}`} aria-labelledby={`verdict-${it.index}`}>
      <Frame tone={kept ? "good" : "bad"}>{kept ? "Kept" : "Not kept"}</Frame>
      <h3 id={`verdict-${it.index}`} className="verdict-title">
        {kept ? "Why the revision was kept" : "The revision did not improve the video"}
      </h3>
      <p className="verdict-reason">{it.reason}</p>
      <Facts
        rows={[
          ["Fresh review", c ? OUTCOME_NAMES[c.outcome] ?? c.outcome : it.outcome ? OUTCOME_NAMES[it.outcome] ?? it.outcome : "n/a"],
          ["Target moment resolved", c?.target_resolved ?? it.target_resolved ?? "n/a"],
          ["Render check", it.change_verified === null ? "n/a" : it.change_verified ? "intended change present" : "intended change missing"],
          ["Preference for the revision", c ? `whole video ${pref(c.full_preference)} · target moment ${pref(c.target_preference)}` : "n/a"],
          ["Reviewer agreement", c?.stability ? `${c.stability.agreeing_runs} of ${c.stability.runs} runs agreed · ${c.stability.order_flips} order flips` : "n/a"],
        ]}
      />
      <ModelJudgment text={(c?.label ?? "fresh reviewer, same criteria, both presentation orders").replace(/^MODEL JUDGMENT:\s*/, "")} />
      <div className="cmp-grid">
        <ListBlock title="Better" items={c?.improved ?? it.improved} empty="Nothing got better." tone="good" />
        <ListBlock title="Worse" items={c?.regressed ?? it.regressed} empty="Nothing got worse." tone="bad" />
        <ListBlock title="Unchanged" items={c?.unchanged ?? []} empty="Not reported." tone="neutral" />
        <ListBlock title="New weak moments" items={c?.new_weaknesses ?? []} empty="None reported." tone="warn" />
      </div>
      {c?.target_evidence.length || repair?.change_verification?.checks.length || c?.notes.length ? (
        <details className="disclosure">
          <summary>Evidence</summary>
          <div className="disclosure-body">
            {c?.target_evidence.length ? (
              <>
                <h5 className="oip-sub">About the target moment</h5>
                <ul className="plain-list">
                  {c.target_evidence.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {repair?.change_verification?.checks.length ? (
              <>
                <h5 className="oip-sub">Render checks</h5>
                <ul className="plain-list mono small">
                  {repair.change_verification.checks.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {c?.notes.length ? (
              <>
                <h5 className="oip-sub">Reviewer notes</h5>
                <ul className="plain-list">
                  {c.notes.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {c?.stability?.agreement ? <p className="muted">{c.stability.agreement}</p> : null}
          </div>
        </details>
      ) : null}
    </section>
  );
}

export function runOutcome(run: RunDetail): { title: string; tone: "good" | "bad" | "neutral" | "warn"; detail: string } {
  const attempts = run.iterations.filter((i) => i.finding_id);
  const kept = attempts.some((i) => i.decision === "accept");
  if (run.status === "running") return { title: "Still running", tone: "warn", detail: "The result appears when the run decides." };
  if (run.status === "failed") return { title: "The run did not finish", tone: "bad", detail: run.stop_reason || run.error || "No reason was recorded." };
  if (kept) return { title: "A revision was kept", tone: "good", detail: run.final_decision };
  if (attempts.some((i) => i.candidate_media_url)) return { title: "The original was kept", tone: "bad", detail: run.final_decision };
  return { title: "No revision was rendered", tone: "neutral", detail: run.final_decision };
}

export function durationLabel(ms: number | null | undefined): string {
  return ms ? fmtS(ms) : "";
}
