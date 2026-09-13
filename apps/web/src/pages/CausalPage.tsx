import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getClient, isNotFound } from "../api/client";
import type { CausalArm, CausalEvidence, CausalRun } from "../api/causal";
import { isTerminal } from "../api/types";
import { Frame, useVideoTitle } from "../components/Shell";
import { ErrorState, Loading } from "../components/States";
import { VideoPlayer, type PlayerHandle } from "../components/VideoPlayer";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { useJob } from "../hooks/useJob";
import { causalCost, causalFailure, causalJobLabel, causalOutcome, incompleteComparison, policyEffectLabel, recordedValue } from "../lib/causal";
import { errorMessage, fmtDateTime, fmtElapsed, fmtS } from "../lib/format";
import { workflowTraceUrl } from "../lib/workflow";
import "./CausalPage.css";

function Lines({ items, empty = "None recorded" }: { items?: string[]; empty?: string }) {
  return items?.length ? <ul className="causal-lines">{items.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="muted small">{empty}</p>;
}

function Fields({ values }: { values: Record<string, unknown> }) {
  return <dl className="causal-facts">{Object.entries(values).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{recordedValue(value)}</dd></div>)}</dl>;
}

function Disclosure({ title, children }: { title: string; children: ReactNode }) {
  return <details className="disclosure causal-disclosure"><summary>{title}</summary><div className="disclosure-body">{children}</div></details>;
}

function Evidence({ ids, evidence, onSeek }: { ids: string[]; evidence: CausalEvidence[]; onSeek: (ms: number) => void }) {
  return <ul className="causal-evidence">{ids.map((id) => {
    const item = evidence.find((e) => e.id === id);
    return <li key={id}><span className="eyebrow">{id} · {item?.source.replaceAll("_", " ") ?? "Evidence unavailable"}</span>
      {item ? <><p>{item.text}</p>{item.start_ms !== null ? <button className="text-link" onClick={() => onSeek(item.start_ms!)}>{fmtS(item.start_ms)}{item.end_ms !== null ? `–${fmtS(item.end_ms)}` : ""} · Watch moment</button> : null}</> : <p>The referenced evidence was not recorded in this dossier.</p>}
    </li>;
  })}</ul>;
}

function Arm({ arm, run }: { arm: CausalArm; run: CausalRun }) {
  const original = useRef<PlayerHandle>(null);
  const candidate = useRef<PlayerHandle>(null);
  const plan = run.plan?.experiments.find((e) => e.id === arm.experiment_id);
  return <article className="causal-arm" id={`arm-${arm.experiment_id}`}>
    <div className="section-line"><Frame>{arm.experiment_id} · {arm.hypothesis_id}</Frame><span className={`result-${arm.verdict}`}>{arm.verdict}</span></div>
    <h3 className="causal-arm-title">{arm.description}</h3>
    <p className="result-explanation">Saved model judgment: {arm.verdict_reason || "A verdict reason has not been recorded."}</p>
    {incompleteComparison(arm) ? <p className="callout">The comparison is incomplete or reports insufficient evidence. The saved verdict above is preserved as recorded; it does not establish a complete comparison or admission to policy memory.</p> : null}
    {arm.render_media_url ? <>
      <div className="cinema-pair">
        <VideoPlayer ref={original} src={run.original_media_url} label="Original · recorded source" />
        <VideoPlayer ref={candidate} src={arm.render_media_url} label={`${arm.experiment_id} · rendered candidate`} />
      </div>
      <div className="comparison-controls"><button className="btn btn-small" onClick={() => { original.current?.seek(0); candidate.current?.seek(0); original.current?.play(); candidate.current?.play(); }}>Play both from start</button><button className="btn btn-small btn-quiet" onClick={() => { original.current?.pause(); candidate.current?.pause(); }}>Pause both</button><span className="small muted">Same playback start; edits may change timing. Mute one player to compare sound.</span></div>
    </> : <p className="callout">No rendered media is recorded for this arm.</p>}
    <Disclosure title="Changes, protected content and side effects"><div className="causal-columns"><div><h4>Changed</h4><Lines items={arm.changes} /></div><div><h4>Protected</h4><Lines items={arm.unchanged} /></div><div><h4>Possible side effects</h4><Lines items={arm.side_effects} /></div></div></Disclosure>
    <Fields values={{ isolation: plan?.isolation, mechanical_verification: arm.verified, evaluation_complete: arm.evaluation_complete, policy_eligible: arm.policy_eligible, candidate_sha256: arm.render_hash }} />
    <Disclosure title="Verification and independent review inputs"><Lines items={arm.checks} /><Fields values={arm.review_inputs} /><h4>Evaluator differences</h4><Lines items={arm.evaluator_differences} /><h4>Learning blockers</h4><Lines items={arm.learning_blockers} />{arm.candidate_audit_id ? <Link className="text-link" to={`/judge/${encodeURIComponent(run.config.video_id)}?audit=${encodeURIComponent(arm.candidate_audit_id)}`}>Inspect the candidate’s saved review</Link> : <p className="muted">No candidate audit recorded.</p>}</Disclosure>
    {arm.axes ? <Disclosure title="Separate model and mechanical comparison axes"><p className="muted small">Model preferences and risk labels are not measured viewer retention or calibrated probabilities.</p><Fields values={arm.axes} /></Disclosure> : null}
    {arm.comparison ? <Disclosure title="Blind comparison evidence"><Fields values={arm.comparison} /></Disclosure> : null}
  </article>;
}

/** Only server records and WORKFLOW_STAGE events populate this session. */
export function CausalPage() {
  const { causalId = "" } = useParams();
  const [params] = useSearchParams();
  const jobId = params.get("job");
  const tracker = useJob(jobId);
  const [run, setRun] = useState<CausalRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [tick, setTick] = useState(0);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const player = useRef<PlayerHandle>(null);
  const title = useVideoTitle(run?.config.video_id ?? "");
  const jobMismatch = Boolean(tracker.job && (tracker.job.kind !== "causal" || tracker.job.causal_id !== causalId));
  const events = jobMismatch ? [] : tracker.events;
  const terminalJob = Boolean(tracker.job && isTerminal(tracker.job.state));
  const active = run?.status === "running" || Boolean(jobId && !jobMismatch && !terminalJob && !tracker.missing);
  const stageCount = events.filter((e) => e.stage === "WORKFLOW_STAGE").length;

  useEffect(() => { setRun(null); setError(null); setMissing(false); setCancelError(null); }, [causalId]);
  useEffect(() => {
    let cancelled = false;
    getClient().then((c) => c.getCausal(causalId)).then((value) => {
      if (!cancelled) { setRun(value); setMissing(false); setError(null); }
    }).catch((err: unknown) => {
      if (cancelled) return;
      if (isNotFound(err)) { setMissing(true); setError(null); }
      else setError(errorMessage(err));
    });
    return () => { cancelled = true; };
  }, [causalId, tick, stageCount, tracker.job?.state]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 4000);
    return () => window.clearInterval(timer);
  }, [active]);

  const cancel = async () => {
    if (!jobId || canceling || jobMismatch) return;
    setCanceling(true); setCancelError(null);
    try { await (await getClient()).cancelJob(jobId); setTick((value) => value + 1); }
    catch (err) { setCancelError(errorMessage(err)); }
    finally { setCanceling(false); }
  };
  const failure = causalFailure({ runError: run?.error, jobError: jobMismatch ? null : tracker.job?.error, stopReason: run?.stop_reason, runFailed: run?.status === "failed", events });
  const stoppedEarly = Boolean(failure && !run?.plan && !run?.arms.length);
  const trace = workflowTraceUrl(run?.weave_url);
  const evidence = run?.dossier?.evidence ?? [];
  const seek = (ms: number) => { player.current?.seek(ms); document.getElementById("causal-observe")?.scrollIntoView({ block: "start" }); };

  return <div className={`page causal-page${failure ? " is-stopped" : ""}`}>
    <header className="study-header">
      <div className="section-line"><Frame>Controlled experiment</Frame><Link className="text-link" to="/runs">All sessions</Link></div>
      <h1 className="editorial-title">{failure?.title ?? (run ? causalOutcome(run) : terminalJob ? "Session stopped" : "Opening the experiment")}</h1>
      <p className="causal-subtitle">{title || run?.config.video_id || causalId}</p>
      <div className="run-meta"><span className="mono">{causalId}</span>{run ? <span>{fmtDateTime(run.created_at)}</span> : null}{run ? <span>{run.audit_source === "recorded" ? "Saved original review reused" : run.audit_source === "fresh" ? "Original reviewed in this session" : "Original review not recorded"}</span> : null}{run?.config.plan_only ? <span>Plan only · rendering disabled</span> : null}{trace ? <a className="text-link" href={trace} target="_blank" rel="noreferrer">Open Weave trace ↗</a> : null}</div>
      <p className="muted small">Model evaluation · sampled frames + ASR</p>
      {!failure ? <Disclosure title="Evaluation basis"><p>Fallible model judgments and mechanical checks. These do not measure human retention.</p></Disclosure> : null}
      {run?.mocked_stages.length ? <p className="callout callout-bad">Test provenance: mocked stages — {run.mocked_stages.join(", ")}. This is not a fully live evaluation.</p> : null}
    </header>
    {jobMismatch ? <p className="callout callout-bad" role="alert">The job in this URL belongs to a different session. Its events and controls are not attached to this record.</p> : null}
    {error ? <ErrorState title="Could not refresh the session" detail={error} onRetry={() => setTick((value) => value + 1)} /> : null}
    {failure ? <><p className="callout callout-bad" role="alert">{failure.message}</p><Disclosure title="Recorded failure details"><Fields values={failure.details} /><p className="muted small">Fallible model judgments and mechanical checks. These do not measure human retention.</p></Disclosure></> : null}
    {!run && !error ? missing && (!jobId || terminalJob || tracker.missing) ? <ErrorState title="No saved session record" detail={tracker.job?.state === "FAILED" ? "The job failed before it saved a causal record. The events below preserve the available evidence." : "The server has no saved result at this address."} onRetry={() => setTick((value) => value + 1)} /> : <Loading what={missing ? "the queued session to save its first record" : "the session"} /> : null}
    {jobId && !jobMismatch ? <div className="causal-live" role="status"><span>{causalJobLabel(tracker.job?.state, tracker.job?.stage)}{tracker.job ? ` · ${fmtElapsed(tracker.elapsedMs)}` : ""} · {tracker.transport === "stream" ? "Live events" : tracker.transport === "polling" ? "Polling events" : "Saved events"}</span>{tracker.job && !terminalJob ? <button className="btn btn-small" disabled={canceling || tracker.job.cancel_requested} onClick={cancel}>{canceling || tracker.job.cancel_requested ? "Cancel requested" : "Cancel session"}</button> : null}{tracker.error ? <span className="small muted">{tracker.error}</span> : null}{cancelError ? <p role="alert">{cancelError}</p> : null}</div> : null}
    {run && !stoppedEarly ? <nav className="chapter-nav causal-chapters" aria-label="Session evidence"><a href="#causal-observe">01 Observe</a><a href="#causal-explain">02 Explain</a><a href="#causal-test">03 Test</a><a href="#causal-learn">04 Learn</a></nav> : null}
    <WorkflowGraph compact stages={run?.workflow_stages} events={events} status={run?.status ?? tracker.job?.state.toLowerCase()} weaveUrl={trace} />
    {run ? <>
      <section id="causal-observe" className="causal-section">
        <div className="causal-observe"><VideoPlayer ref={player} src={run.original_media_url} label="Original" /><div>
          <Frame>01 · Observed symptom</Frame>
          <h2>{run.dossier?.symptom.description || "Review incomplete"}</h2>
          {run.dossier ? <p className="mono">{fmtS(run.dossier.symptom.start_ms)}–{fmtS(run.dossier.symptom.end_ms)} · {run.dossier.symptom.severity} · {run.dossier.symptom.kind.replaceAll("_", " ")}</p> : null}
          <Disclosure title="Goal"><p>{run.config.objective}</p></Disclosure>
          {run.audit_id ? <Link className="text-link" to={`/judge/${encodeURIComponent(run.config.video_id)}?audit=${encodeURIComponent(run.audit_id)}`}>Inspect the original review and attention timeline</Link> : null}
          <Disclosure title="Source identity and constraints"><Fields values={{ sha256: run.artifact_hash || null, original_review: run.audit_source, launched_via: run.launched_via, runtime_version: run.version }} /><Lines items={run.config.constraints} /></Disclosure>
        </div></div>
        {run.dossier?.open_loops.length ? <Disclosure title={`Expectations and payoffs · ${run.dossier.open_loops.length} observations`}><div className="causal-loop-list">{run.dossier.open_loops.map((loop, i) => <div key={i}><p>{loop.expectation}</p><p className="small muted">Opened {loop.established_ms === null ? "at an unrecorded time" : fmtS(loop.established_ms)} · {loop.resolved_ms === null ? "No resolution recorded" : `Resolution at ${fmtS(loop.resolved_ms)}`} · {loop.risk_rises_at_ms === null ? "No associated risk rise recorded" : `Model risk rises at ${fmtS(loop.risk_rises_at_ms)}`}</p></div>)}</div></Disclosure> : null}
      </section>
      {!stoppedEarly ? <><section id="causal-explain" className="causal-section"><Frame>02 · Competing explanations</Frame><h2>What could explain this moment?</h2><p className="muted">Evidence strength describes support for a hypothesis. It is not a probability that the hypothesis is true.</p>
        {run.plan?.hypotheses.map((hypothesis) => <article className="causal-hypothesis" key={hypothesis.id}><div><span className="eyebrow">{hypothesis.id} · {hypothesis.cause_type.replaceAll("_", " ")}</span><h3>{hypothesis.statement}</h3><p>{hypothesis.status} · {hypothesis.strength} support · {hypothesis.testable ? "An edit can test this" : "No executable test available"}</p></div><Disclosure title={`${hypothesis.evidence_for.length} supporting · ${hypothesis.evidence_against.length} against`}><Lines items={hypothesis.why_not_high} empty="" /><div className="causal-columns"><div><h4>Supporting evidence</h4>{hypothesis.evidence_for.length ? <Evidence ids={hypothesis.evidence_for} evidence={evidence} onSeek={seek} /> : <p className="muted">None recorded</p>}</div><div><h4>Counterevidence</h4>{hypothesis.evidence_against.length ? <Evidence ids={hypothesis.evidence_against} evidence={evidence} onSeek={seek} /> : <p className="muted">None recorded</p>}</div></div></Disclosure></article>)}
        {!run.plan ? <p className="muted">No competing hypotheses have been saved yet.</p> : <p className="callout">{run.plan.uncertainty || "No uncertainty statement recorded."}</p>}
      </section>
      <section id="causal-test" className="causal-section"><Frame>03 · Isolate and compare</Frame><h2>{run.plan?.decision === "do_not_edit" ? "The evidence does not justify an edit" : "Test the explanation against the original"}</h2><p className="causal-lead">{run.plan?.decision_reason || "No edit decision recorded yet."}</p>
        {run.plan?.experiments.length ? <div className="causal-experiments">{run.plan.experiments.map((experiment) => <Disclosure key={experiment.id} title={`${experiment.id} · ${experiment.hypothesis_id} · ${experiment.description}${run.plan!.chosen.includes(experiment.id) ? " · Selected" : " · Not selected"}`}><Fields values={{ isolation: experiment.isolation, separates_competing_causes: experiment.discriminating, also_tests: experiment.also_tests, rank_without_memory: experiment.rank_without_policy, rank_with_memory: experiment.rank, priority_without_memory: experiment.priority_without_policy, priority_with_memory: experiment.priority }} /><div className="causal-columns"><div><h4>Changed</h4><Lines items={experiment.changes} /></div><div><h4>Unchanged</h4><Lines items={experiment.unchanged} /></div><div><h4>Side effects</h4><Lines items={experiment.side_effects} /></div></div><h4>Prior evidence admitted ({experiment.prior.matches.length})</h4>{experiment.prior.matches.map((match, i) => <Fields key={i} values={match} />)}<h4>Prior evidence excluded ({experiment.prior.excluded.length})</h4>{experiment.prior.excluded.map((match, i) => <Fields key={i} values={match} />)}</Disclosure>)}</div> : null}
        {run.arms.map((arm) => <Arm key={arm.experiment_id} arm={arm} run={run} />)}
        {!run.arms.length ? <p className="callout">{run.config.plan_only ? "This session was requested as a plan only. No variant was rendered or compared." : run.status === "running" ? "No experiment arm has been saved yet." : "No experiment arm is recorded for this session."}</p> : null}
      </section>
      </> : null}
      <section id="causal-learn" className="causal-section"><Frame>04 · Conclusion and memory</Frame><h2>{stoppedEarly ? "Saved progress" : causalOutcome(run)}</h2><Disclosure title="Conclusion and stopping reason"><p>{run.stop_reason || "No stopping reason recorded."}</p><Lines items={run.conclusions} empty="No final conclusions recorded." /></Disclosure>
        {run.plan?.policy_effect ? <><h3>{policyEffectLabel(run.plan.policy_effect)}</h3><p>{run.plan.policy_effect.summary}</p><Fields values={{ ranking_changed: run.plan.policy_effect.order_changed, selected_experiments_changed: run.plan.policy_effect.selection_changed, first_choice_changed: run.plan.policy_effect.first_choice_changed }} /><Disclosure title="Recorded score and ranking changes"><Lines items={run.plan.policy_effect.score_changes} /><Lines items={run.plan.policy_effect.rank_changes} /></Disclosure></> : null}
        <Fields values={{ policy_records_before: run.policy_before.length, policy_records_after: run.policy_after.length, new_policy_records: run.policy_records_added.length, model_calls: run.budget.model_calls, allowed_model_calls: run.budget.max_model_calls, recorded_cost: causalCost(run.budget.cost_usd), elapsed_seconds: run.budget.elapsed_s }} />
        <p className="small muted">{recordedValue(run.budget.cost_note)}</p>
        <Disclosure title="Policy records and evaluator provenance"><Fields values={{ added_records: run.policy_records_added, before: run.policy_before, after: run.policy_after, hypothesis_results: run.hypothesis_results }} /><Fields values={run.runtime} /></Disclosure>
        {run.plan ? <Disclosure title="How the planner computed support and priority"><Fields values={run.plan.formulas} /></Disclosure> : null}
      </section>
    </> : null}
  </div>;
}
