import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import type { AuditFinding, AuditStrength, WindowReaction } from "../api/types";
import { readAttention, REACTION_WORDS, reviewed, RISK_WORDS, type AttentionStory } from "../lib/attention";
import { fmtRange, fmtS } from "../lib/format";
import { packLanes, pct } from "../lib/intervals";
import { ModelJudgment } from "./Judge";

const range = (w: WindowReaction) => <span className="mono">{fmtRange(w.start_ms, w.end_ms)}</span>;
const riskWord = (w: WindowReaction) => RISK_WORDS[w.attention_risk]?.toLowerCase() ?? w.attention_risk;
const reactionWord = (w: WindowReaction) => REACTION_WORDS[w.reaction] ?? w.reaction;

/** Plain sentences over the window predictions, at exactly the granularity the review returned. */
function Summary({ story }: { story: AttentionStory }): ReactNode {
  const { windows: ws, reviewedCount, maxRisk, firstRise, firstStretch, peakRuns, recovery, endsElevated } = story;
  if (!ws.length) return <>No moment-by-moment reading was recorded for this review.</>;
  if (!reviewedCount) return <>The moment-by-moment reviews failed, so there is no attention reading.</>;
  const missing = ws.length - reviewedCount;
  const missingNote = missing ? ` ${missing} window${missing === 1 ? " was" : "s were"} not reviewed.` : "";
  const whole = <span className="mono">{fmtRange(ws[0].start_ms, ws[ws.length - 1].end_ms)}</span>;
  if (maxRisk === "low" || firstRise === null || !firstStretch) {
    return (
      <>
        Low predicted risk in all {reviewedCount} reviewed windows, {whole}.{missingNote}
      </>
    );
  }
  const rise = ws[firstRise];
  const stretchEnd = ws[firstStretch[1]];
  const firstPeak = peakRuns[0];
  const riseIsPeak = firstPeak && firstPeak[0] === firstRise;
  const riseRange = riseIsPeak ? <span className="mono">{fmtRange(rise.start_ms, ws[firstPeak[1]].end_ms)}</span> : range(rise);
  const parts: ReactNode[] = [];
  if (firstRise === 0) {
    parts.push(
      <span key="rise">
        Starts at {riskWord(rise)} risk, {riseRange} ({reactionWord(rise)}).
      </span>,
    );
  } else {
    parts.push(
      <span key="rise">
        Low risk until <span className="mono">{fmtS(rise.start_ms)}</span>, then {riskWord(rise)} at {riseRange} ({reactionWord(rise)}).
      </span>,
    );
  }
  const laterPeaks = riseIsPeak ? peakRuns.slice(1) : peakRuns;
  if (laterPeaks.length) {
    const p = laterPeaks[0];
    parts.push(
      <span key="peak">
        {" "}
        Highest: {maxRisk} at <span className="mono">{fmtRange(ws[p[0]].start_ms, ws[p[1]].end_ms)}</span>
        {laterPeaks.length > 1 ? `, and ${laterPeaks.length - 1} more stretch${laterPeaks.length - 1 === 1 ? "" : "es"} at that level` : ""}.
      </span>,
    );
  }
  if (recovery !== null) {
    parts.push(
      <span key="recovery">
        {" "}
        Back to low at {range(ws[recovery])}.
      </span>,
    );
  } else if (endsElevated) {
    parts.push(
      <span key="end">
        {" "}
        Still {riskWord(ws.filter(reviewed).pop() ?? stretchEnd)} at the end.
      </span>,
    );
  }
  if (missingNote) parts.push(<span key="missing">{missingNote}</span>);
  return <>{parts}</>;
}

interface Props {
  windows: WindowReaction[];
  durationMs: number;
  findings: AuditFinding[];
  strengths: AuditStrength[];
  cursorMs: number;
  selectedFindingId: string | null;
  onSelectFinding: (f: AuditFinding) => void;
  onSeek: (ms: number) => void;
  onPlay: (start: number, end: number) => void;
}

/**
 * Model-predicted attention risk: one column per reviewed window, bar height by the predicted risk label,
 * with the weak moments and the strengths to keep on the same time axis.
 */
export function AttentionChart({ windows, durationMs, findings, strengths, cursorMs, selectedFindingId, onSelectFinding, onSeek, onPlay }: Props) {
  const titleId = useId();
  const story = useMemo(() => readAttention(windows), [windows]);
  const ws = story.windows;
  const defaultIndex = story.peakRuns.length && story.maxRisk !== "low" ? story.peakRuns[0][0] : null;
  const [selected, setSelected] = useState<number | null>(defaultIndex);
  useEffect(() => setSelected(defaultIndex), [windows, defaultIndex]);
  const d = durationMs || (ws.length ? ws[ws.length - 1].end_ms : 0);
  const boundaries = ws.length ? [...ws.map((w) => w.start_ms), ws[ws.length - 1].end_ms] : [];
  const stride = Math.max(1, Math.ceil(boundaries.length / 12));
  const packedFindings = packLanes(findings);
  const findingLanes = Math.max(1, ...packedFindings.map((p) => p.lane + 1));
  const numberOf = (id: string) => findings.findIndex((f) => f.id === id) + 1;
  const activeIndex = ws.findIndex(w => cursorMs >= w.start_ms && cursorMs < w.end_ms);
  const sel = activeIndex >= 0 ? ws[activeIndex] : selected !== null ? ws[selected] ?? null : null;

  const choose = (i: number) => {
    setSelected(i);
    onSeek(ws[i].start_ms);
  };

  return (
    <section className="attn" aria-labelledby={titleId}>
      <div className="attn-head">
        <h2 id={titleId} className="section-name">
          Model-predicted attention risk
        </h2>
        <ModelJudgment text="predicted attention risk, not measured retention" />
      </div>
      <details className="attention-summary-more"><summary>Read the attention summary</summary><p className="attn-summary"><Summary story={story} /></p></details>
      {ws.length ? (
        <div className="attn-chart">
          <div className="attn-rows">
            <div className="attn-yaxis" aria-hidden="true">
              <span>High</span>
              <span>Medium</span>
              <span>Low</span>
            </div>
            <div className="attn-plot">
              <div className="attn-grid" aria-hidden="true">
                <i />
                <i />
                <i />
              </div>
              {ws.map((w, i) => {
                const left = pct(w.start_ms, d);
                const width = pct(w.end_ms, d) - left;
                const ok = reviewed(w);
                return (
                  <button
                    key={`${w.start_ms}-${w.end_ms}`}
                    type="button"
                    className={`attn-col risk-${ok ? w.attention_risk : "unknown"}${activeIndex === i ? " is-selected" : ""}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    aria-pressed={activeIndex === i}
                    aria-label={`${fmtRange(w.start_ms, w.end_ms)}: ${ok ? `${riskWord(w)} predicted attention risk, ${reactionWord(w)}` : "not reviewed"}`}
                    onClick={() => choose(i)}
                  >
                    <span className="attn-bar" aria-hidden="true">
                      <span className="attn-label">
                        <span className="attn-label-risk">{ok ? RISK_WORDS[w.attention_risk] : "No reading"}</span>
                        {ok ? <span className="sr-only">{reactionWord(w)}</span> : null}
                      </span>
                    </span>
                  </button>
                );
              })}
              <span className="attn-playhead" style={{ left: `${pct(cursorMs, d)}%` }} aria-hidden="true" />
            </div>
          </div>
          <div className="attn-rows">
            <span className="attn-lane-label" aria-hidden="true" />
            <div className="attn-xaxis" aria-hidden="true">
              {boundaries.map((t, i) =>
                i % stride === 0 || i === boundaries.length - 1 ? (
                  <span key={`${t}-${i}`} style={{ left: `${pct(t, d)}%` }} className={i === boundaries.length - 1 ? "is-end" : i === 0 ? "is-start" : ""}>
                    {(t / 1000).toFixed(1)}
                    {i === boundaries.length - 1 ? " s" : ""}
                  </span>
                ) : null,
              )}
            </div>
          </div>
          {findings.length ? (
            <div className="attn-rows">
              <span className="attn-lane-label">Weak moments</span>
              <div className="attn-lane" style={{ height: `${findingLanes * 1.7}rem` }}>
                {packedFindings.map(({ item: f, lane }) => {
                  const left = pct(f.start_ms, d);
                  const width = Math.max(pct(f.end_ms, d) - left, 1.5);
                  return (
                    <button
                      key={f.id}
                      type="button"
                      className={`attn-mark sev-${f.severity}${selectedFindingId === f.id ? " is-selected" : ""}`}
                      style={{ left: `${left}%`, width: `${width}%`, top: `${lane * 1.7}rem` }}
                      aria-pressed={selectedFindingId === f.id}
                      aria-label={`Weak moment ${numberOf(f.id)}, ${fmtRange(f.start_ms, f.end_ms)}: ${f.weakness}`}
                      onClick={() => onSelectFinding(f)}
                    >
                      <span className="mono">{numberOf(f.id)}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}
          {strengths.length ? (
            <div className="attn-rows">
              <span className="attn-lane-label">Keep</span>
              <div className="attn-lane attn-lane-keep">
                {strengths.map((s) => {
                  const left = pct(s.start_ms, d);
                  const width = Math.max(pct(s.end_ms, d) - left, 1);
                  return (
                    <button
                      key={s.id}
                      type="button"
                      className="attn-keep"
                      style={{ left: `${left}%`, width: `${width}%` }}
                      aria-label={`Keep ${fmtRange(s.start_ms, s.end_ms)}: ${s.what}`}
                      title={`Keep ${fmtRange(s.start_ms, s.end_ms)}: ${s.what}`}
                      onClick={() => onPlay(s.start_ms, s.end_ms)}
                    />
                  );
                })}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
      {sel ? (
        <div className="attn-detail" aria-live="polite">
          <div className="attn-detail-head">
            <span className="mono attn-detail-when">{fmtRange(sel.start_ms, sel.end_ms)}</span>
            <span className={`attn-risk-tag risk-${reviewed(sel) ? sel.attention_risk : "unknown"}`}>{reviewed(sel) ? `${RISK_WORDS[sel.attention_risk]} predicted risk` : "Not reviewed"}</span>
            {reviewed(sel) ? <span className="attn-detail-reaction">{reactionWord(sel)}</span> : null}
            <button type="button" className="btn btn-quiet btn-small" onClick={() => onPlay(sel.start_ms, sel.end_ms)}>
              Play this window
            </button>
          </div>
          {sel.error ? (
            <p className="callout callout-warn">This window could not be reviewed: {sel.error}</p>
          ) : (
            <dl className="attn-kv">
              <dt>Understands</dt>
              <dd>{sel.understanding || "Not stated."}</dd>
              <dt>Expects next</dt>
              <dd>{sel.expectation || "Not stated."}</dd>
              {sel.open_question ? (
                <>
                  <dt>Open question</dt>
                  <dd>{sel.open_question}</dd>
                </>
              ) : null}
              <dt>Possible cause</dt>
              <dd>{sel.cause || "Not stated."}</dd>
            </dl>
          )}
        </div>
      ) : ws.length ? (
        <p className="attn-hint">Choose a window to see what the cold viewer understood, expected and why.</p>
      ) : null}
    </section>
  );
}
