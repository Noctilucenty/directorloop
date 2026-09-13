import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getClient, isNotFound } from "../api/client";
import { isTerminal, type Audit, type Repair, type RunDetail } from "../api/types";
import { AttemptCard, Lessons } from "../components/Attempts";
import { ExternalIcon } from "../components/Icons";
import { EvaluationProtocol } from "../components/Judge";
import { JudgeView } from "../components/JudgeView";
import { LinkedPlayers, RevisionVerdict, runOutcome } from "../components/Result";
import { EventLog, RunRail } from "../components/RunRail";
import { Frame, useVideoTitle } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { Stepper, type StepKey, type StepView } from "../components/Stepper";
import { VideoPlayer } from "../components/VideoPlayer";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { useAppState } from "../hooks/useAppState";
import { useJob } from "../hooks/useJob";
import { errorMessage, fmtDateTime, fmtElapsed, fmtRange, newIdempotencyKey } from "../lib/format";
import { ROUTE_NAMES } from "../lib/labels";

const RELOAD_ON = new Set(["AUDIT_DONE", "DECIDED", "DONE", "FAILED", "CANCELED"]);

export function RunPage() {
  const { runId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const app = useAppState();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [runMissing, setRunMissing] = useState(false);
  // Only runs started through the API have a job with live events. A stored run launched from the command line has none,
  // so its job is not requested; a record that does not exist yet (a run that is just starting) still is.
  const stateJobId = (location.state as { jobId?: string } | null)?.jobId ?? null;
  const derivedJobId = runId ? `job_${runId.replace(/^run_/, "")}` : null;
  const jobId = stateJobId ?? (run ? (run.launched_via === "api" ? derivedJobId : null) : runMissing ? derivedJobId : null);
  const tracker = useJob(jobId);
  const [runError, setRunError] = useState<string | null>(null);
  const [audits, setAudits] = useState<Record<string, Audit>>({});
  const [repairs, setRepairs] = useState<Record<string, Repair>>({});
  const [tick, setTick] = useState(0);
  const [compareIndex, setCompareIndex] = useState<number | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);

  // The stored run record. It may not exist yet in the first seconds of a run.
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    getClient()
      .then((c) => c.getRun(runId))
      .then((r) => {
        if (cancelled) return;
        setRun(r);
        setRunMissing(false);
        setRunError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (isNotFound(err)) setRunMissing(true);
        else setRunError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, tick]);

  // Reload the record when the job reports a milestone, and every few seconds while it runs.
  const lastStage = tracker.events.length ? tracker.events[tracker.events.length - 1].stage : null;
  useEffect(() => {
    if (lastStage && RELOAD_ON.has(lastStage)) setTick((t) => t + 1);
  }, [lastStage, tracker.events.length]);
  const jobRunning = tracker.job !== null && !isTerminal(tracker.job.state);
  useEffect(() => {
    if (!jobRunning) return;
    const t = window.setInterval(() => setTick((x) => x + 1), 5000);
    return () => window.clearInterval(t);
  }, [jobRunning]);

  // Audits and repairs referenced by the record or by live events.
  const eventAuditIds = tracker.events.map((e) => e.data?.audit_id).filter((x): x is string => typeof x === "string");
  const eventRepairIds = tracker.events.map((e) => e.data?.repair_run_id).filter((x): x is string => typeof x === "string");
  const wantedAudits = useMemo(
    () => [...new Set([...(run?.audit_ids ?? []), ...(run?.iterations ?? []).flatMap((i) => [i.audit_id, i.candidate_audit_id ?? ""]), ...eventAuditIds].filter(Boolean))],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [run, eventAuditIds.join()],
  );
  const wantedRepairs = useMemo(
    () => [...new Set([...(run?.repair_run_ids ?? []), ...(run?.iterations ?? []).map((i) => i.repair_run_id ?? ""), ...eventRepairIds].filter(Boolean))],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [run, eventRepairIds.join()],
  );
  useEffect(() => {
    let cancelled = false;
    getClient().then(async (c) => {
      for (const id of wantedAudits) {
        if (audits[id]) continue;
        try {
          const a = await c.getAudit(id);
          if (!cancelled) setAudits((prev) => ({ ...prev, [id]: a }));
        } catch {
          // an audit still being written appears on a later reload
        }
      }
      for (const id of wantedRepairs) {
        if (repairs[id]) continue;
        try {
          const r = await c.getRepair(id);
          if (!cancelled) setRepairs((prev) => ({ ...prev, [id]: r }));
        } catch {
          // same as above
        }
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantedAudits.join(), wantedRepairs.join()]);

  const videoId = run?.config.video_id ?? (tracker.job?.params?.video_id as string | undefined) ?? null;
  const title = useVideoTitle(videoId);
  const originalAuditId = run?.iterations[0]?.audit_id ?? run?.audit_ids[0] ?? eventAuditIds[0] ?? null;
  const original = originalAuditId ? audits[originalAuditId] ?? null : null;
  const findings = useMemo(() => [...(original?.findings ?? [])].sort((a, b) => a.start_ms - b.start_ms), [original]);
  const attempts = run?.iterations ?? [];
  const tried = attempts.filter((i) => i.finding_id);
  const withCandidate = attempts.filter((i) => i.candidate_media_url);
  const comparing = withCandidate.find((i) => i.index === compareIndex) ?? withCandidate[withCandidate.length - 1] ?? null;
  const judgeDone = Boolean(original) || eventAuditIds.length > 0;
  const runDone = run ? run.status !== "running" : tracker.job ? isTerminal(tracker.job.state) : false;
  const failed = run?.status === "failed" || tracker.job?.state === "FAILED" || tracker.job?.state === "CANCELED";

  const steps: StepView[] = [
    {
      key: "judge",
      number: "01",
      title: "Judge",
      state: original ? "done" : failed && !judgeDone ? "failed" : jobRunning ? "working" : run ? "done" : "waiting",
      status: original ? `${findings.length} weak moment${findings.length === 1 ? "" : "s"}` : jobRunning ? "Watching as a cold viewer" : failed ? "Did not finish" : "Waiting",
    },
    {
      key: "improve",
      number: "02",
      title: "Improve",
      state: runDone ? (failed ? "failed" : "done") : judgeDone && jobRunning ? "working" : "waiting",
      status: runDone
        ? tried.length
          ? `${tried.length} edit${tried.length === 1 ? "" : "s"} tried, ${tried.filter((i) => i.decision === "accept").length} kept`
          : "No edit was possible"
        : judgeDone && jobRunning
          ? "Trying edits"
          : "Waiting",
    },
    {
      key: "result",
      number: "03",
      title: "Result",
      state: runDone ? (failed ? "failed" : "done") : "waiting",
      status: run && runDone ? runOutcome(run).title : "Waiting",
    },
  ];

  const stepParam = params.get("step") as StepKey | null;
  const derived: StepKey = !runDone && judgeDone && jobRunning && tracker.events.some((e) => e.stage === "SELECTING") ? "improve" : "judge";
  const step: StepKey = stepParam ?? derived;
  const pick = (key: StepKey) => setParams((p) => {
    const next = new URLSearchParams(p);
    next.set("step", key);
    return next;
  }, { replace: true });

  const lastDecided = [...attempts].reverse().find((i) => i.decision !== "stop") ?? null;
  const canTryAnother = Boolean(run && runDone && !failed && app.feature("runs") && attempts.length && attempts[attempts.length - 1].next_action !== "stop");
  const tryAnother = async () => {
    if (!run) return;
    setRetrying(true);
    setRetryError(null);
    try {
      const c = await getClient();
      const started = await c.startRun({ video_id: run.config.video_id, objective: run.config.objective, constraints: run.config.constraints, focus: run.config.focus, allowed_edits: run.config.allowed_edits, iteration_budget: run.config.iteration_budget, idempotency_key: newIdempotencyKey() });
      navigate(`/runs/${encodeURIComponent(started.run_id)}`, { state: { jobId: started.job_id } });
    } catch (err) {
      setRetryError(errorMessage(err));
    } finally {
      setRetrying(false);
    }
  };

  if (runError && !run) return <ErrorState title="Could not load this run" detail={runError} onRetry={() => setTick((t) => t + 1)} />;
  if (!run && runMissing && tracker.missing) return <EmptyState title="Run not found" detail={`There is no run called ${runId}.`} action={<Link className="btn" to="/runs">All runs</Link>} />;
  if (!run && !tracker.job) return <Loading what="the run" />;

  const mediaUrl = run?.original_media_url ?? (videoId ? `/media/source/${videoId}.mp4` : null);
  const outcome = run ? runOutcome(run) : null;

  return (
    <div className="run">
      <header className="run-head">
        <div className="run-title-row">
          <h1 className="run-title">{title || "Run"}</h1>
          {run ? <span className={`status-chip status-${run.status}`}>{run.status === "running" ? "Running" : run.status === "completed" ? "Completed" : "Failed"}</span> : null}
          {run?.weave_url ? (
            <a className="btn btn-quiet btn-small run-trace" href={run.weave_url} target="_blank" rel="noreferrer">
              View trace <ExternalIcon size={14} />
            </a>
          ) : null}
        </div>
        <div className="run-meta">
          <span className="mono">{runId}</span>
          {run ? <span>{fmtDateTime(run.created_at)}</span> : null}
          {run?.timings_ms.total_ms ? <span className="mono">{fmtElapsed(run.timings_ms.total_ms)}</span> : null}
          {run ? <span className="run-objective" title={run.config.objective}>{run.config.objective}</span> : null}
        </div>
      </header>

      <Stepper steps={steps} active={step} onPick={pick} />

      {run?.annotations.length ? (
        <div className="note-after" role="note">
          <Frame tone="warn">Note added after this run</Frame>
          {run.annotations.map((a) => (
            <p key={a}>{a}</p>
          ))}
        </div>
      ) : null}

      {step === "judge" ? (
        <section className="panel-step" aria-label="Judge">
          {original ? (
            <>
              <JudgeView audit={original} mediaUrl={mediaUrl} />
              {runDone || jobRunning ? (
                <div className="next-cta">
                  <div>
                    <Frame>02 Improve</Frame>
                    <p className="next-line">
                      {jobRunning ? "The run is trying edits now." : tried.length ? `${tried.length} edit${tried.length === 1 ? " was" : "s were"} tried on this video.` : "No edit this system can make fits these weak moments."}
                    </p>
                  </div>
                  <button type="button" className="btn btn-primary btn-hero" onClick={() => pick("improve")}>
                    {jobRunning ? "Follow the improvement" : "See how it tried to improve this video"}
                  </button>
                </div>
              ) : null}
            </>
          ) : (
            <div className="judge">
              <div className="judge-media">
                <VideoPlayer src={mediaUrl} className="player-hero" />
              </div>
              <div className="judge-panel">
                {tracker.job ? (
                  <RunRail job={tracker.job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} />
                ) : failed ? (
                  <ErrorState title="The run stopped before the review finished" detail={run?.stop_reason || run?.error || undefined} />
                ) : (
                  <Loading what="the review" />
                )}
              </div>
            </div>
          )}
        </section>
      ) : null}

      {step === "improve" ? (
        <section className="panel-step improve" aria-label="Improve">
          <div className="improve-grid">
            <div className="improve-rail">
              {tracker.job ? (
                <RunRail job={tracker.job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} />
              ) : (
                <div className="rail rail-static">
                  <Frame>Execution</Frame>
                  <p className="muted">{run?.launched_via === "cli" ? "This run was launched from the command line, so no live events were recorded. The history on the right comes from its stored record." : "No live events are available for this run."}</p>
                  {run ? (
                    <dl className="kv">
                      <dt>Started</dt>
                      <dd>{fmtDateTime(run.created_at)}</dd>
                      <dt>Ended</dt>
                      <dd>{fmtDateTime(run.ended_at)}</dd>
                      <dt>Budget</dt>
                      <dd>
                        {run.config.iteration_budget} attempt{run.config.iteration_budget === 1 ? "" : "s"}
                      </dd>
                    </dl>
                  ) : null}
                </div>
              )}
            </div>
            <div className="attempts">
              {attempts.length === 0 ? (
                jobRunning ? <p className="muted">The first attempt appears here when it is decided.</p> : <EmptyState title="No attempts were recorded" detail={run?.stop_reason} />
              ) : (
                attempts.map((it) => (
                  <AttemptCard
                    key={it.index}
                    it={it}
                    repair={it.repair_run_id ? repairs[it.repair_run_id] ?? null : null}
                    onCompare={(index) => {
                      setCompareIndex(index);
                      pick("result");
                    }}
                  />
                ))
              )}
              {run ? <Lessons lessons={run.lessons} /> : null}
            </div>
          </div>
          <WorkflowGraph run={run} events={tracker.events} status={tracker.job?.state ?? run?.status} mode="single" />
        </section>
      ) : null}

      {step === "result" ? (
        <section className="panel-step result" aria-label="Result">
          {!run || !runDone ? (
            <EmptyState title="The result is not ready" detail="It appears when the run finishes." />
          ) : (
            <>
              <div className={`outcome tone-${outcome?.tone}`}>
                <Frame tone={outcome?.tone === "good" ? "good" : outcome?.tone === "bad" ? "bad" : undefined}>03 Result</Frame>
                <h2 className="outcome-title">{outcome?.title}</h2>
                <p className="outcome-detail">{outcome?.detail}</p>
                {run.stop_reason ? <p className="outcome-stop">Stopped because: {run.stop_reason}</p> : null}
              </div>
              {comparing ? (
                <>
                  {withCandidate.length > 1 ? (
                    <div className="seg" role="radiogroup" aria-label="Attempt to compare">
                      {withCandidate.map((i) => (
                        <button key={i.index} type="button" role="radio" aria-checked={comparing.index === i.index} className={`seg-btn${comparing.index === i.index ? " is-on" : ""}`} onClick={() => setCompareIndex(i.index)}>
                          Attempt {i.index}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <div className="result-grid">
                    <LinkedPlayers
                      originalSrc={run.original_media_url}
                      revisionSrc={comparing.candidate_media_url as string}
                      revisionLabel={`Revision ${comparing.candidate_version_id ?? ""}`.trim()}
                      map={comparing.repair_run_id ? repairs[comparing.repair_run_id]?.interval_map ?? [] : []}
                      target={comparing.considered.find((c) => c.finding_id === comparing.finding_id)?.interval_ms ?? null}
                    />
                    <RevisionVerdict it={comparing} repair={comparing.repair_run_id ? repairs[comparing.repair_run_id] ?? null : null} />
                  </div>
                </>
              ) : (
                <div className="no-revision">
                  <p>Nothing was rendered, so there is no revision to compare. These weak moments need something this system cannot make from the file alone:</p>
                  <ul className="needs">
                    {findings.map((f) => (
                      <li key={f.id}>
                        <span className="mono">{fmtRange(f.start_ms, f.end_ms)}</span>
                        <span>{f.weakness}</span>
                        {f.repair ? <span className={`route-chip route-${f.repair.route}`}>{ROUTE_NAMES[f.repair.route] ?? f.repair.route}</span> : null}
                        {f.repair?.required_materials.length ? <span className="muted small">Would need: {f.repair.required_materials.join("; ")}</span> : null}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {canTryAnother ? (
                <div className="next-cta">
                  <p className="next-line">{lastDecided?.next_action_reason || "The run left room for another attempt."}</p>
                  <button type="button" className="btn btn-primary btn-hero" onClick={tryAnother} disabled={retrying}>
                    Try another hypothesis
                  </button>
                </div>
              ) : null}
              {retryError ? (
                <p className="callout callout-bad" role="alert">
                  {retryError}
                </p>
              ) : null}
            </>
          )}
        </section>
      ) : null}

      <section className="details" aria-label="Details">
        <details className="disclosure">
          <summary>Evaluation protocol</summary>
          <div className="disclosure-body protocol-grid">
            {wantedAudits.map((id) => audits[id]).filter(Boolean).map((a, i) => (
              <EvaluationProtocol key={a.id} audit={a} title={i === 0 ? "Review of the original" : `Fresh review ${a.version_id}`} />
            ))}
            {Object.values(repairs).map((r) =>
              r.comparison ? (
                <div key={r.id} className="protocol">
                  <h5 className="oip-sub">Comparison</h5>
                  <p>{r.comparison.label}</p>
                  {r.comparison.stability ? <p className="muted">{r.comparison.stability.agreement}</p> : null}
                </div>
              ) : null,
            )}
          </div>
        </details>
        <details className="disclosure">
          <summary>Agent trace</summary>
          <div className="disclosure-body">
            <ul className="plain-list">
              {run?.weave_url ? (
                <li>
                  <a className="text-link" href={run.weave_url} target="_blank" rel="noreferrer">
                    Whole run in Weave <ExternalIcon size={14} />
                  </a>
                </li>
              ) : (
                <li className="muted">No Weave trace was recorded for this run.</li>
              )}
              {Object.values(repairs).map((r) =>
                r.weave_url ? (
                  <li key={r.id}>
                    <a className="text-link" href={r.weave_url} target="_blank" rel="noreferrer">
                      Repair {r.id} <ExternalIcon size={14} />
                    </a>
                  </li>
                ) : null,
              )}
            </ul>
            {!jobId && run ? (
              <p className="muted">This run was launched from the {run.launched_via === "cli" ? "command line" : run.launched_via}, so it has no job event log. Its stored record is shown above.</p>
            ) : tracker.missing ? (
              <p className="muted">No job exists for this run in the API, so there is no event log.</p>
            ) : (
              <EventLog events={tracker.events} />
            )}
          </div>
        </details>
        <details className="disclosure">
          <summary>Technical details</summary>
          <div className="disclosure-body">
            {run ? (
              <dl className="kv">
                <dt>Run</dt>
                <dd className="mono">{run.id}</dd>
                <dt>Launched via</dt>
                <dd>{run.launched_via}</dd>
                <dt>Final version</dt>
                <dd className="mono">{run.final_version_id}</dd>
                <dt>Constraints</dt>
                <dd>{run.config.constraints.length ? run.config.constraints.join("; ") : "none"}</dd>
                <dt>Allowed edits</dt>
                <dd className="mono">{run.config.allowed_edits?.join(", ") ?? "all"}</dd>
                <dt>Mocked stages</dt>
                <dd>{run.mocked_stages.length ? run.mocked_stages.join(", ") : "none"}</dd>
                <dt>Manual steps</dt>
                <dd>{run.manual_interventions.length ? run.manual_interventions.join("; ") : "none"}</dd>
                {Object.entries(run.runtime)
                  .filter(([, v]) => typeof v !== "object" || v === null)
                  .map(([k, v]) => (
                    <div key={k} className="kv-row">
                      <dt>{k.replace(/_/g, " ")}</dt>
                      <dd className="mono">{String(v)}</dd>
                    </div>
                  ))}
                {(["reviewer", "selector"] as const).map((role) => {
                  const r = run.runtime[role] as { provider?: string; model?: string; reasoning_effort?: string } | undefined;
                  return r ? (
                    <div key={role} className="kv-row">
                      <dt>{role}</dt>
                      <dd className="mono">
                        {r.provider} {r.model} {r.reasoning_effort ? `(${r.reasoning_effort})` : ""}
                      </dd>
                    </div>
                  ) : null;
                })}
              </dl>
            ) : (
              <p className="muted">The run record is not written yet.</p>
            )}
          </div>
        </details>
      </section>
    </div>
  );
}
