import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Arm, DesignPreview, FitnessComponent, Hypothesis, PolicyUpdate, Ranking, StrategySnapshot, WeakRegion } from "../api/types";
import { fmtRange, fmtS, fmtShare } from "../lib/format";
import { armName, componentName, familyName, mutationName } from "../lib/labels";
import { EvidenceBadge, Glyph, OutcomePill, StrategyStatusPill } from "./Badge";

export function WeakRegionLine({ region, retentionNote }: { region: WeakRegion | null; retentionNote: string | null }) {
  if (!region) return <p className="muted">No weak region was found by the structural detectors.</p>;
  const measured = region.basis.includes("measured");
  return (
    <div className="weak">
      <p className="weak-sentence">
        Observed symptom · suspected region <span className="mono">{fmtRange(region.start_ms, region.end_ms)}</span>
      </p>
      <div className="weak-basis">
        <span className={`basis basis-${measured ? "measured" : "structure"}`}>{measured ? "from a measured drop" : "from creative structure"}</span>
        <EvidenceBadge kind={region.evidence_class} />
      </div>
      {retentionNote ? <p className="muted small">{retentionNote}</p> : null}
    </div>
  );
}

export function HypothesisCard({ h, rank, onSeek }: { h: Hypothesis; rank: number; onSeek?: (ms:number) => void }) {
  return (
    <article className="hyp">
      <div className="hyp-head">
        <span className="hyp-rank mono">{String(rank).padStart(2, "0")}</span>
        <h3 className="hyp-family">{familyName(h.family)}</h3>
        {onSeek ? <button className="text-button mono" onClick={() => onSeek(h.region_start_ms)}>{fmtRange(h.region_start_ms, h.region_end_ms)}</button> : <span className="mono muted small">{fmtRange(h.region_start_ms, h.region_end_ms)}</span>}
      </div>
      <p className="hyp-statement">{h.statement}</p>
      <div className="confidence" aria-label={`Detection confidence ${h.detection_confidence.toFixed(2)}`}>
        <span className="confidence-label">Detection confidence</span>
        <span className="confidence-track">
          <span className="confidence-fill" style={{ width: `${Math.round(h.detection_confidence * 100)}%` }} />
        </span>
        <span className="mono">{h.detection_confidence.toFixed(2)}</span>
      </div>
      <div className="hyp-evidence-columns">
        <details className="hyp-evidence"><summary><span>For</span><b>{h.evidence.length} observation{h.evidence.length === 1 ? "" : "s"}</b></summary>
          <ul className="ev-list">{h.evidence.map((e,i) => <li key={i}><EvidenceBadge kind={e.kind} /><span>{e.text}</span></li>)}</ul>
        </details>
        <details className="hyp-evidence"><summary><span>Against</span><b>{h.counterevidence.length} observation{h.counterevidence.length === 1 ? "" : "s"}</b></summary>
          {h.counterevidence.length ? <ul className="ev-list">{h.counterevidence.map((e,i) => <li key={i}><EvidenceBadge kind={e.kind} /><span>{e.text}</span></li>)}</ul> : <p className="muted">No counterevidence recorded.</p>}
        </details>
      </div>
    </article>
  );
}

export function RankingList({ rankings, arms }: { rankings: Ranking[]; arms: Arm[] | null }) {
  const armFor = (mutation: string) => arms?.find((a) => a.mutation?.type === mutation) ?? null;
  return (
    <ol className="ranking">
      {rankings.map((r, i) => {
        const arm = armFor(r.mutation);
        return (
          <li key={`${r.mutation}-${r.hypothesis_id}`} className={`rank-row${r.selected ? "" : " is-skipped"}`}>
            <span className="rank-n mono">{i + 1}</span>
            <div className="rank-body">
              <div className="rank-title">
                <span className="rank-name">{mutationName(r.mutation)}</span>
                {arm ? <span className="arm-chip">{armName(arm.label)}</span> : r.selected ? <span className="arm-chip">selected</span> : <span className="arm-chip arm-chip-off">not run</span>}
              </div>
              <div className="rank-reason" title={r.reason}>
                <span>
                  detection <b className="mono">{r.detection_confidence.toFixed(2)}</b>
                </span>
                <span>
                  reference prior <b className="mono">{r.reference_support === null ? "n/a" : r.reference_support.toFixed(2)}</b>
                </span>
                {r.reason.includes("ignored") ? (
                  <span>experiment evidence ignored</span>
                ) : (
                  <span>
                    evidence <b className="mono">{r.policy_mean === null ? "n/a" : r.policy_mean.toFixed(2)}</b>{" "}
                    <span className="mono">
                      {r.policy_wins}W {r.policy_losses}L {r.policy_neutral}N
                    </span>
                  </span>
                )}
              </div>
            </div>
            <span className="rank-score mono">{r.score.toFixed(3)}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function MemoryComparison({ none, learned }: { none: DesignPreview | null; learned: DesignPreview | null }) {
  if (!none || !learned) return <p className="muted">Ranking previews are not available for this video.</p>;
  const noneRank = new Map(none.rankings.map((r, i) => [r.mutation, i]));
  const same = none.rankings.map((r) => r.mutation).join() === learned.rankings.map((r) => r.mutation).join();
  const rows = Math.max(none.rankings.length, learned.rankings.length);
  return (
    <div className="memory">
      <div className="memory-grid" role="table" aria-label="Ranking without memory and with the learned policy">
        <div className="memory-col-head" role="columnheader">
          No memory
        </div>
        <div className="memory-col-head" role="columnheader">
          Learned policy <span className="mono">v{learned.policy_version}</span>
        </div>
        {Array.from({ length: rows }).map((_, i) => {
          const a = none.rankings[i];
          const b = learned.rankings[i];
          const before = b ? noneRank.get(b.mutation) : undefined;
          const delta = before === undefined ? 0 : before - i;
          return (
            <div key={i} className={`memory-row${i === 0 ? " is-first" : ""}`} role="row">
              <div className="memory-cell" role="cell">
                {a ? (
                  <>
                    <span className="rank-n mono">{i + 1}</span>
                    <span className="memory-name">{mutationName(a.mutation)}</span>
                    <span className="mono muted">{a.score.toFixed(3)}</span>
                  </>
                ) : null}
              </div>
              <div className="memory-cell" role="cell">
                {b ? (
                  <>
                    <span className="rank-n mono">{i + 1}</span>
                    <span className="memory-name">{mutationName(b.mutation)}</span>
                    <span className="mono muted">{b.score.toFixed(3)}</span>
                    <span className={`delta ${delta > 0 ? "delta-up" : delta < 0 ? "delta-down" : "delta-flat"}`} aria-label={delta === 0 ? "same rank" : delta > 0 ? `up ${delta}` : `down ${-delta}`}>
                      {delta > 0 ? <Glyph shape="up" /> : delta < 0 ? <Glyph shape="down" /> : <Glyph shape="dash" />}
                      {delta === 0 ? "" : Math.abs(delta)}
                    </span>
                  </>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
      {same ? (
        <p className="muted small">
          Same order.{" "}
          {learned.policy_version === 0 ? "No experiment evidence existed at policy v0, so memory has nothing to change yet." : "Recorded evidence did not change the ranking."}
        </p>
      ) : (
        <p className="small">
          Memory moved <strong>{mutationName(learned.rankings[0]?.mutation)}</strong> to the top
          {none.rankings[0] ? (
            <>
              {" "}
              ahead of <strong>{mutationName(none.rankings[0].mutation)}</strong>
            </>
          ) : null}
          .
        </p>
      )}
    </div>
  );
}

function PreferenceMeter({ value }: { value: number }) {
  const left = Math.min(value, 0.5) * 100;
  const width = Math.abs(value - 0.5) * 100;
  const tone = value > 0.5 ? "good" : value < 0.5 ? "bad" : "flat";
  return (
    <span className={`pref pref-${tone}`} aria-hidden="true">
      <span className="pref-mid" />
      <span className="pref-fill" style={{ left: `${left}%`, width: `${Math.max(width, 0.8)}%` }} />
    </span>
  );
}

function valueText(c: FitnessComponent): string {
  if (c.value === null) return "not tested";
  if (c.unit === "ms") return fmtS(c.value);
  if (c.unit.startsWith("share of") && c.unit.includes("calls")) return fmtShare(c.value);
  if (c.unit === "share of trials") return `${fmtShare(c.value)}`;
  if (c.unit.startsWith("of ")) return `${c.value} ${c.unit}`;
  return `${c.value} ${c.unit}`;
}

function unitNote(c: FitnessComponent): string {
  if (c.value === null) return c.detail;
  if (c.unit.startsWith("share of") && c.unit.includes("calls")) return `${c.valid ?? "?"} calls, both orders`;
  if (c.unit === "share of trials") return `${c.valid ?? c.trials ?? "?"} trials`;
  if (c.unit.startsWith("of ")) return `${c.valid ?? "?"} answers`;
  return "";
}

function Delta({ c, control }: { c: FitnessComponent; control: FitnessComponent | undefined }) {
  if (!control || control.value === null || c.value === null || c.unit !== control.unit) return null;
  const diff = c.value - control.value;
  if (Math.abs(diff) < 1e-9) return <span className="fdelta fdelta-flat">same</span>;
  const better = c.better === "lower" ? diff < 0 : diff > 0;
  const text = c.unit === "ms" ? `${diff > 0 ? "+" : "-"}${fmtS(Math.abs(diff))}` : `${diff > 0 ? "+" : "-"}${Math.abs(diff).toFixed(2)}`;
  return <span className={`fdelta ${better ? "fdelta-good" : "fdelta-bad"}`}>{text}</span>;
}

export function ArmCard({ arm, control, active, onSelect }: { arm: Arm; control: Arm | null; active: boolean; onSelect: () => void }) {
  const isControl = arm.label === "control";
  const comps = arm.fitness?.components ?? [];
  const groups = ["model_eval", "human_test", "mechanical", "reference", "historical", "real", "simulated"]
    .map((kind) => ({ kind, rows: comps.filter((c) => c.evidence === kind) }))
    .filter((g) => g.rows.length);
  const controlComp = (name: string) => control?.fitness?.components.find((c) => c.name === name);
  const tradeoff = arm.outcome === "loss" && arm.outcome_reason?.includes("tradeoff") ? arm.outcome_reason.slice(arm.outcome_reason.indexOf("tradeoff")) : null;
  const mainReason = tradeoff && arm.outcome_reason ? arm.outcome_reason.slice(0, arm.outcome_reason.indexOf("tradeoff")).replace(/[;,\s]+$/, "") : arm.outcome_reason;
  return (
    <article className={`arm${arm.outcome ? ` arm-${arm.outcome}` : ""}${active ? " is-active" : ""}`} aria-label={armName(arm.label)}>
      <header className="arm-head">
        <button type="button" className="arm-label" onClick={onSelect} aria-pressed={active} aria-label={`Play ${armName(arm.label)}`}>
          {isControl ? "Control" : arm.label}
        </button>
        {isControl ? <span className="pill pill-none">BASELINE</span> : <OutcomePill outcome={arm.outcome} large />}
      </header>
      <div className="arm-mutation">
        <div className="arm-mutation-name">{isControl ? "The original, rendered through the same pipeline" : mutationName(arm.mutation?.type)}</div>
        {arm.mutation ? <p className="arm-desc">{arm.mutation.description}</p> : null}
        {arm.mutation ? (
          <div className="meta-row">
            <span className="meta-label">Changed</span>
            <span>{arm.mutation.changed_variable}</span>
          </div>
        ) : null}
        {arm.mutation?.protected_variables.length ? (
          <details className="protected">
            <summary>Held constant ({arm.mutation.protected_variables.length})</summary>
            <ul className="plain-list">
              {arm.mutation.protected_variables.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
      {!isControl ? (
        <div className="arm-reason">
          {mainReason ? <p>{mainReason}</p> : <p className="muted">No reason recorded.</p>}
          {tradeoff ? <p className="tradeoff">{tradeoff.charAt(0).toUpperCase() + tradeoff.slice(1)}</p> : null}
        </div>
      ) : null}
      {arm.fitness && !arm.fitness.hard_gates_passed ? (
        <div className="callout callout-bad small">Hard gate failed: {arm.fitness.gate_notes.join("; ") || "no detail"}</div>
      ) : null}
      {groups.map((g) => (
        <div key={g.kind} className="fitness-group">
          <div className="fitness-group-head">
            <EvidenceBadge kind={g.kind} />
          </div>
          <table className="fitness">
            <tbody>
              {g.rows.map((c) => {
                const pref = c.unit.startsWith("share of") && c.unit.includes("calls") && c.value !== null;
                return (
                  <tr key={c.name} title={c.detail}>
                    <th scope="row">{componentName(c.name)}</th>
                    <td className="fvalue">
                      <span className="mono">{valueText(c)}</span>
                      {pref ? <PreferenceMeter value={c.value as number} /> : null}
                      {!isControl ? <Delta c={c} control={controlComp(c.name)} /> : null}
                      <span className="fnote">{unitNote(c)}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
      {!isControl && (arm.preference_reasons.full.length || arm.preference_reasons.hook.length) ? (
        <details className="reasons">
          <summary>What the model said ({arm.preference_reasons.full.length + arm.preference_reasons.hook.length})</summary>
          {arm.preference_reasons.full.length ? <div className="meta-label">Full video</div> : null}
          <ul className="plain-list small">
            {arm.preference_reasons.full.map((r, i) => (
              <li key={`f${i}`}>{r}</li>
            ))}
          </ul>
          {arm.preference_reasons.hook.length ? <div className="meta-label">First 3 s</div> : null}
          <ul className="plain-list small">
            {arm.preference_reasons.hook.map((r, i) => (
              <li key={`h${i}`}>{r}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

function counts(s: StrategySnapshot): string {
  return `W${s.wins ?? 0} L${s.losses ?? 0} N${s.neutral ?? 0} R${s.rejected ?? 0}`;
}

export function PolicyUpdates({ updates }: { updates: PolicyUpdate[] }) {
  if (!updates.length) return <p className="muted">This run did not record anything in the policy.</p>;
  return (
    <ol className="pchanges">
      {updates.map((u) => (
        <li key={`${u.policy_version}-${u.strategy}`} className="pchange">
          <span className="pchange-v mono">v{u.policy_version}</span>
          <span className="pchange-name">{mutationName(u.mutation)}</span>
          <OutcomePill outcome={u.outcome} />
          <span className="pchange-status">
            <StrategyStatusPill status={String(u.before.status ?? "PROPOSED")} />
            <span className="arrow" aria-label="became">
              to
            </span>
            <StrategyStatusPill status={String(u.after.status ?? "PROPOSED")} />
          </span>
          <span className="pchange-counts mono">
            {counts(u.before)} <span className="muted">to</span> {counts(u.after)}
          </span>
        </li>
      ))}
    </ol>
  );
}

export function ExperimentLink({ id, children }: { id: string; children?: ReactNode }) {
  return (
    <Link className="mono link" to={`/research/experiments/${encodeURIComponent(id)}`}>
      {children ?? id}
    </Link>
  );
}
