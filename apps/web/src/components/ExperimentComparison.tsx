import { useRef } from "react";
import type { Arm } from "../api/types";
import { fmtS, fmtShare } from "../lib/format";
import { mutationName } from "../lib/labels";
import { EvidenceBadge } from "./Badge";
import { ArmCard } from "./Experiment";
import { VideoPlayer, type PlayerHandle } from "./VideoPlayer";

export function ExperimentComparison({ original, candidate }: { original: Arm | null; candidate: Arm }) {
  const a = useRef<PlayerHandle>(null);
  const b = useRef<PlayerHandle>(null);
  const metric = (arm: Arm | null, name: string) => arm?.fitness?.components.find(c => c.name === name);
  const preference = metric(candidate, "model_full_preference_vs_control");
  const comprehension = metric(candidate, "message_comprehension");
  const originalComprehension = metric(original, "message_comprehension");
  const payoff = metric(candidate, "payoff_ms");
  const originalPayoff = metric(original, "payoff_ms");
  const comparableComprehension = comprehension?.value != null && originalComprehension?.value != null && comprehension.unit === originalComprehension.unit;
  const outcome = candidate.outcome ?? (candidate.status === "failed" ? "incomplete" : "inconclusive");
  return <div className="experiment-comparison">
    <div className="comparison-cinema">
      <div className="cinema-pair">
        <div><VideoPlayer ref={a} src={original?.media_url ?? null} label="Original" /><p className="media-caption">{originalPayoff?.value != null ? `Payoff ${fmtS(originalPayoff.value)}` : "Payoff timing unavailable"}</p></div>
        <div><VideoPlayer ref={b} src={candidate.media_url} label={`Variant ${candidate.label}`} muted /><p className="media-caption">{payoff?.value != null ? `Payoff ${fmtS(payoff.value)}` : "Payoff timing unavailable"}</p></div>
      </div>
      <div className="comparison-controls"><button className="btn" onClick={() => { a.current?.seek(0); b.current?.seek(0); a.current?.play(); b.current?.play(); }}>Play both from start</button><button className="btn btn-quiet" onClick={() => { a.current?.pause(); b.current?.pause(); }}>Pause both</button></div>
      <p className="muted small">Sound from original. Use each player to inspect different timings.</p>
      <div className="edit-mutation"><span className="eyebrow">Changed</span><h3>{mutationName(candidate.mutation?.type)}</h3><p>{candidate.mutation?.description ?? "No mutation description recorded."}</p>
        {originalPayoff?.value != null && payoff?.value != null && originalPayoff.value !== payoff.value ? <p className="mutation-time">{fmtS(originalPayoff.value)} <span>→</span> {fmtS(payoff.value)}</p> : null}
      </div>
      {candidate.mutation ? <div className="frozen"><span className="eyebrow">Everything else frozen</span><p className="muted">Controlled edit plan · protected variables</p><ul>{candidate.mutation.protected_variables.map(p => <li key={p}>{p}</li>)}</ul><p className="muted">Known edit risk: {candidate.mutation.risk || "not recorded"}</p></div> : null}
    </div>
    <div className="comparison-evidence">
      <span className="eyebrow">Did it work?</span><h2 className={`result-word result-${outcome}`}>{outcome.toUpperCase()}</h2><p className="result-explanation">{candidate.outcome_reason ?? "No completed decision was recorded for this variant."}</p>
      <div className="preference-result"><EvidenceBadge kind="model_eval" /><h3>Continue-watching preference</h3>
        {preference?.value != null ? <><div className="preference-number">{fmtShare(preference.value)}<span>variant preference</span></div><div className="preference-track"><i style={{width:`${Math.max(0,Math.min(1,preference.value))*100}%`}} /></div><p className="muted">{preference.unit} · {preference.detail}</p></> : <p className="muted">No preference evaluation recorded.</p>}
      </div>
      <dl className="result-facts"><div><dt>Comprehension</dt><dd>{comparableComprehension ? comprehension.value === originalComprehension.value ? "Unchanged" : `${originalComprehension.value} → ${comprehension.value} ${comprehension.unit}` : "Not evaluated"}</dd></div><div><dt>Hard gates</dt><dd>{candidate.fitness ? candidate.fitness.hard_gates_passed ? "PASS" : "FAIL" : "Not evaluated"}</dd></div></dl>
      {candidate.fitness?.gate_notes.length ? <p className="callout">{candidate.fitness.gate_notes.join("; ")}</p> : null}
      <p className="muted small">Model evaluation. No platform retention measurement is implied.</p>
      <details className="disclosure"><summary>Exact metrics, model reasons, and side effects</summary><div className="disclosure-body"><ArmCard arm={candidate} control={original} active onSelect={() => b.current?.play()} /></div></details>
    </div>
  </div>;
}
