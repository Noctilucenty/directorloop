import { useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { getClient } from "../api/client";
import type { TransferReport } from "../api/types";
import { EvidenceBadge, OutcomePill, RunBadge } from "../components/Badge";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { LoopSculpture } from "../components/Motion";
import { ResearchNav } from "../components/Shell";
import { useAppState } from "../hooks/useAppState";
import { useAsync } from "../hooks/useAsync";
import { fmtElapsed, fmtStamp } from "../lib/format";
import { mutationName } from "../lib/labels";
import { rankChange, transferEvidence } from "../lib/transfer";

export function TransferPanel({ report }: { report: TransferReport }) {
  const { videos } = useAppState();
  const policy = useAsync(() => getClient().then(c => c.getPolicy()), []);
  const evidence = transferEvidence(report, policy.data);
  const source = evidence[0];
  const promoted = report.modes.learned.first_mutation;
  const [selected, setSelected] = useState<string | null>(null);
  const [reveal, setReveal] = useState(true);
  const learnedList = useRef<HTMLOListElement>(null);
  const positions = useRef(new Map<string,number>());
  useLayoutEffect(() => {
    const next = new Map<string,number>();
    const animations: Animation[] = [];
    learnedList.current?.querySelectorAll<HTMLElement>("[data-rank]").forEach(el => {
      const key = el.dataset.rank!;
      const top = el.offsetTop;
      const previous = positions.current.get(key);
      if(previous !== undefined && previous !== top && document.documentElement.dataset.motion !== "paused" && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) animations.push(el.animate([{transform:`translateY(${previous-top}px)`},{transform:"translateY(0)"}],{duration:700,easing:"cubic-bezier(.22,1,.36,1)"}));
      next.set(key,top);
    });
    positions.current = next;
    return () => animations.forEach(a => a.cancel());
  },[reveal,report]);
  const focus = selected ?? promoted;
  const change = promoted ? rankChange(promoted, report.modes.none.order, report.modes.learned.order) : null;
  const titleOf = (id: string) => videos.find(v => v.video_id === id)?.title ?? id;
  const count = Math.max(report.modes.none.order.length, report.modes.learned.order.length);
  const focusEvidence = evidence.filter(e => e.mutation === focus);
  const wins = Object.values(report.outcomes_on_b).filter(o => o === "win").length;
  const sourceChange = source ? rankChange(source.mutation, report.modes.none.order, report.modes.learned.order) : null;
  return <div className="memory-story">
    <div className="memory-story-meta"><RunBadge kind="recorded" /><EvidenceBadge kind="model_eval" /><span>Rankings saved before evaluation · {fmtStamp(report.created_at)}</span></div>
    <div className="transfer-chain" aria-label="Past video experiment, creative memory, next video experiment">
      <div className="chain-source"><span className="eyebrow">Video A · past experiment</span><h2>{source ? titleOf(source.video_id) : "Prior experiment unavailable"}</h2>{source ? <><p>{mutationName(source.mutation)}</p><OutcomePill outcome={source.outcome} large /><Link className="text-link" to={`/research/experiments/${encodeURIComponent(source.experiment_id)}`}>Inspect the evidence <span aria-hidden="true">↗</span></Link></> : <p className="muted">{policy.loading ? "Loading prior evidence…" : "No prior matching experiments."}</p>}</div>
      <span className="chain-arrow" aria-hidden="true">→</span>
      <div className="chain-memory" data-tilt><LoopSculpture compact /><span className="eyebrow">Creative memory</span><h2>Evidence,<br />carried forward.</h2><span className="mono">Policy v{report.policy_version}</span></div>
      <span className="chain-arrow" aria-hidden="true">→</span>
      <div className="chain-destination"><span className="eyebrow">Video B · next experiment</span><h2>{titleOf(report.video_id)}</h2><p>{mutationName(promoted)}</p><div className="rank-promotion">{change?.before != null && change.after != null ? <><span>#{change.before}</span><i aria-hidden="true">→</i>#{change.after}</> : "No ranking"}</div><Link className="text-link" to={`/judge/${encodeURIComponent(report.video_id)}`}>Inspect Video B <span aria-hidden="true">↗</span></Link></div>
    </div>
    <div className="ranking-reveal-head"><div><span className="eyebrow">The next plan</span><h2>{report.first_choice_changed ? "Previous evidence changed the plan." : "Memory kept the same first choice."}</h2></div><button className="btn btn-quiet" onClick={() => setReveal(r => !r)} aria-pressed={reveal}>{reveal ? "Hide memory effect" : "Reveal memory effect"}</button></div>
    <div className={`rank-transfer ${reveal ? "is-revealed" : ""}`}>
      <div className="rank-transfer-column"><h3 className="eyebrow">Without memory</h3><ol>{report.modes.none.order.map((m,i) => <li key={m}><button className={focus === m ? "is-focused" : ""} onClick={() => setSelected(m)}><span className="rank-number">{String(i+1).padStart(2,"0")}</span><span>{mutationName(m)}</span></button></li>)}</ol></div>
      <div className="rank-wires" aria-hidden="true"><svg viewBox={`0 0 200 ${Math.max(1,count)*80}`} preserveAspectRatio="none">{report.modes.none.order.map((m,i) => { const j = report.modes.learned.order.indexOf(m); if(j < 0) return null; return <path key={m} className={focus === m ? "is-focused" : ""} d={`M 0 ${i*80+40} C 90 ${i*80+40} 110 ${(reveal ? j : i)*80+40} 200 ${(reveal ? j : i)*80+40}`} />; })}</svg></div>
      <div className="rank-transfer-column learned"><h3 className="eyebrow">{reveal ? "With DirectorLoop memory" : "Without memory · before transfer"}</h3><ol ref={learnedList}>{(reveal ? report.modes.learned.order : report.modes.none.order).map((m,i) => { const r = rankChange(m,report.modes.none.order,report.modes.learned.order); return <li key={m} data-rank={m}><button className={focus === m ? "is-focused" : ""} onClick={() => setSelected(m)}><span className="rank-number">{String(i+1).padStart(2,"0")}</span><span>{mutationName(m)}</span>{reveal && r.delta !== null && r.delta !== 0 ? <small>{r.delta > 0 ? "↑" : "↓"} from #{r.before}</small> : null}</button></li>; })}</ol></div>
    </div>
    <div className="transfer-explanation"><span className="eyebrow">Why did the plan change?</span>{source ? <p><strong>{mutationName(source.mutation)}</strong> had a recorded <strong>{source.outcome.toUpperCase()}</strong> on Video A.{sourceChange?.delta != null && sourceChange.delta < 0 ? ` It moved from #${sourceChange.before} to #${sourceChange.after} on Video B.` : " That outcome entered the saved policy."} {promoted && report.first_choice_changed ? `${mutationName(promoted)} became the first choice.` : ""}</p> : <p>No prior matching experiments are available to explain this saved ranking.</p>}<EvidenceBadge kind="policy_evidence" /> <span className="muted">{new Set(evidence.map(e => e.experiment_id)).size} prior matching experiment{new Set(evidence.map(e => e.experiment_id)).size === 1 ? "" : "s"}</span></div>
    <details className="disclosure"><summary>Evidence for {mutationName(focus)}</summary><div className="disclosure-body">{focusEvidence.length ? focusEvidence.map(e => <div key={`${e.experiment_id}-${e.mutation}`}><p>{e.note}</p><p>Conditions: {e.scope.replace(/[_:]/g," ")} · Policy v{e.policyVersion} · {e.outcome.toUpperCase()}</p><Link className="text-link" to={`/research/experiments/${encodeURIComponent(e.experiment_id)}`}>Source experiment</Link></div>) : <p>No prior matching experiments for this mutation. Its position can change when evidence lowers another candidate.</p>}<p className="muted">Similarity and individual score contributions are not included in this saved transfer report.</p></div></details>
    <div className="transfer-outcome"><div><span className="eyebrow">What happened on Video B?</span><h2>{report.modes.learned.first_outcome_on_b?.toUpperCase() ?? "INCONCLUSIVE"}</h2></div><div><p>{wins ? `${wins} of ${Object.keys(report.outcomes_on_b).length} variants won in the recorded evaluation.` : "The changed plan did not produce a winning variant."}</p><p className="muted">First choice without memory: {report.modes.none.first_outcome_on_b?.toUpperCase() ?? "not evaluated"}. First choice with memory: {report.modes.learned.first_outcome_on_b?.toUpperCase() ?? "not evaluated"}.</p><Link className="text-link" to={`/research/experiments/${encodeURIComponent(report.oracle_experiment)}`}>Inspect the Video B experiment</Link></div></div>
    <details className="disclosure"><summary>Recorded outcomes, costs, and traces</summary><div className="disclosure-body"><ul>{Object.entries(report.outcomes_on_b).map(([m,o]) => <li key={m}>{mutationName(m)} · {o.toUpperCase()}</li>)}</ul><p>Without memory: {report.modes.none.model_calls_until_then} model calls · {fmtElapsed(report.modes.none.eval_and_render_ms_until_then)} · {report.modes.none.note}</p><p>With memory: {report.modes.learned.model_calls_until_then} model calls · {fmtElapsed(report.modes.learned.eval_and_render_ms_until_then)} · {report.modes.learned.note}</p>{report.weave_url ? <a className="text-link" href={report.weave_url} target="_blank" rel="noreferrer">Transfer trace</a> : null}<pre>{JSON.stringify(report,null,2)}</pre></div></details>
    {policy.error ? <ErrorState title="Prior evidence could not be loaded" detail={policy.error} onRetry={policy.reload} /> : null}
  </div>;
}

export function TransferPage() {
  const reports = useAsync(() => getClient().then(c => c.listTransfers()), []);
  const [index,setIndex] = useState(0);
  const report = reports.data?.[index];
  return <div className="page memory-page"><header className="memory-page-head"><div><span className="eyebrow">Evidence memory</span><h1>Every video teaches <span>the next experiment.</span></h1></div><p>A past experiment becomes evidence.<br />That evidence changes what gets tested next.</p></header>
    {reports.loading ? <Loading what="evidence memory" /> : null}{reports.error ? <ErrorState title="Could not load memory transfer" detail={reports.error} onRetry={reports.reload} /> : null}
    {reports.data?.length === 0 ? <EmptyState title="No prior matching experiments" detail="Complete an experiment, then analyze a new video to inspect whether memory changes the plan." action={<Link to="/" className="btn">Analyze a video</Link>} /> : null}
    {reports.data && reports.data.length > 1 ? <label className="memory-test-picker">Transfer test <select value={index} onChange={e => setIndex(Number(e.target.value))}>{reports.data.map((r,i) => <option key={`${r.video_id}-${r.created_at}`} value={i}>{r.video_id} · {fmtStamp(r.created_at)}</option>)}</select></label> : null}
    {report ? <TransferPanel key={`${report.video_id}-${report.created_at}`} report={report} /> : null}
    <footer className="memory-support"><ResearchNav /></footer>
  </div>;
}
