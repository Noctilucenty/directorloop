import { useEffect, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { getClient, isNotFound } from "../api/client";
import { ABC_STAGES, isTerminal, type ABCRun } from "../api/types";
import { CAttemptCard, DimensionTable, finalWords, PairPlayers, usageLine, VerdictChip, WhatWorks } from "../components/Compare";
import { ExternalIcon } from "../components/Icons";
import { ModelJudgment } from "../components/Judge";
import { EventLog } from "../components/RunRail";
import { Frame } from "../components/Shell";
import { StageRail } from "../components/StageRail";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { Stepper, type StepKey, type StepView } from "../components/Stepper";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { useJob } from "../hooks/useJob";
import { errorMessage, fmtDateTime, fmtElapsed } from "../lib/format";

const RELOAD = new Set(["AUDITS_DONE", "COMPARED_AB", "DECIDED_C", "DONE", "FAILED", "CANCELED"]);

export function ComparePage() {
  const { abcId = "" } = useParams();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const [run, setRun] = useState<ABCRun | null>(null);
  const [missing, setMissing] = useState(false);
  // Only comparisons started through the API have a job with live events; command-line records are read from storage alone.
  const stateJobId = (location.state as { jobId?: string } | null)?.jobId ?? null;
  const derivedJobId = abcId ? `job_${abcId.replace(/^abc_/, "")}` : null;
  const jobId = stateJobId ?? (run ? (run.launched_via === "api" ? derivedJobId : null) : missing ? derivedJobId : null);
  const tracker = useJob(jobId);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getClient()
      .then((c) => c.getAbc(abcId))
      .then((r) => {
        if (cancelled) return;
        setRun(r);
        setMissing(false);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (isNotFound(err)) setMissing(true);
        else setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [abcId, tick]);

  const last = tracker.events.length ? tracker.events[tracker.events.length - 1].stage : null;
  useEffect(() => {
    if (last && RELOAD.has(last)) setTick((t) => t + 1);
  }, [last, tracker.events.length]);
  const running = tracker.job !== null && !isTerminal(tracker.job.state);
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setTick((x) => x + 1), 5000);
    return () => window.clearInterval(t);
  }, [running]);

  if (error && !run) return <ErrorState title="Could not load this comparison" detail={error} onRetry={() => setTick((t) => t + 1)} />;
  if (!run && missing && tracker.missing) return <EmptyState title="Comparison not found" detail={`There is no comparison called ${abcId}.`} action={<Link className="btn" to="/runs">All runs</Link>} />;
  if (!run && !tracker.job) return <Loading what="the comparison" />;

  const comparison = run?.comparison ?? null;
  const done = run ? run.status !== "running" : false;
  const failed = run?.status === "failed" || tracker.job?.state === "FAILED";
  const attempts = run?.attempts ?? [];
  const tried = attempts.filter((a) => a.proposal);
  const steps: StepView[] = [
    { key: "judge", number: "01", title: "What works in each", state: comparison ? "done" : failed ? "failed" : running ? "working" : "waiting", status: comparison ? (comparison.best_supported ? `${comparison.best_supported} preferred overall` : "No reliable preference") : failed ? "Did not finish" : "Waiting" },
    { key: "improve", number: "02", title: "Build C", state: done ? (failed ? "failed" : "done") : comparison && running ? "working" : "waiting", status: done ? (tried.length ? `${tried.length} C tried, ${attempts.filter((a) => a.decision === "accept").length} kept` : failed ? "Not reached" : "No C was built") : "Waiting" },
    { key: "result", number: "03", title: "Result", state: done ? (failed ? "failed" : "done") : "waiting", status: run && done ? finalWords(run).title : "Waiting" },
  ];
  const step = (params.get("step") as StepKey | null) ?? "judge";
  const pick = (key: StepKey) => setParams({ step: key }, { replace: true });
  const outcome = run ? finalWords(run) : null;

  return (
    <div className="run">
      <header className="run-head">
        <div className="run-title-row">
          <h1 className="run-title">Compare A and B, then build C</h1>
          {run ? <span className={`status-chip status-${run.status}`}>{run.status === "running" ? "Running" : run.status === "completed" ? "Completed" : "Failed"}</span> : null}
          {run?.weave_url ? (
            <a className="btn btn-quiet btn-small run-trace" href={run.weave_url} target="_blank" rel="noreferrer">
              View trace <ExternalIcon size={14} />
            </a>
          ) : null}
        </div>
        <div className="run-meta">
          <span className="mono">{abcId}</span>
          {run ? <span>{fmtDateTime(run.created_at)}</span> : null}
          {run?.timings_ms.total_ms ? <span className="mono">{fmtElapsed(run.timings_ms.total_ms)}</span> : null}
          {run ? <span className="run-objective" title={run.context.objective}>{run.context.objective}</span> : null}
        </div>
      </header>

      <Stepper steps={steps} active={step} onPick={pick} />

      {failed ? (
        <div className="outcome tone-bad">
          <Frame tone="bad">Failed</Frame>
          <h2 className="outcome-title">The comparison did not finish</h2>
          <p className="outcome-detail">{run?.stop_reason || tracker.job?.error || "No reason was recorded."}</p>
          {run?.error ? <p className="mono small muted">{run.error}</p> : null}
        </div>
      ) : null}
      {run?.annotations.length ? (
        <div className="note-after" role="note">
          <Frame tone="warn">Note added after this comparison</Frame>
          {run.annotations.map((a) => (
            <p key={a}>{a}</p>
          ))}
        </div>
      ) : null}
      {running && tracker.job ? <StageRail job={tracker.job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} expected={[...ABC_STAGES]} /> : null}

      {run && step === "judge" ? (
        <section className="panel-step" aria-label="What works in each">
          <div className="compare-top">
            <PairPlayers run={run} />
            <div className="compare-verdicts">
              {comparison?.whole ? (
                <>
                  <div className="best">
                    <span className="muted">Preferred overall</span>
                    <VerdictChip verdict={comparison.whole.overall.verdict} />
                  </div>
                  <p className="best-reason">{comparison.best_supported_reason}</p>
                  <ModelJudgment text={comparison.whole.label.replace(/^MODEL JUDGMENT:\s*/, "")} />
                  {!comparison.comparable || comparison.confounds.length ? (
                    <p className="callout callout-warn">
                      {comparison.comparable ? "" : "The two versions may not be directly comparable. "}
                      {comparison.confounds.join("; ")}
                    </p>
                  ) : null}
                  <DimensionTable comparison={comparison.whole} questions={run.rubric.whole_dimensions} />
                </>
              ) : (
                <EmptyState title="No comparison yet" detail={failed ? "The run stopped before A and B were compared." : "It appears when both reviews finish."} />
              )}
            </div>
          </div>
          {comparison ? <WhatWorks comparison={comparison} /> : null}
          {comparison?.regions.length ? (
            <details className="disclosure">
              <summary>Stretch by stretch</summary>
              <div className="disclosure-body regions">
                {comparison.regions.map((r, i) => (
                  <div key={i} className="region">
                    <h5 className="oip-sub">Part {i + 1}</h5>
                    <DimensionTable comparison={r} questions={run.rubric.region_dimensions} />
                  </div>
                ))}
              </div>
            </details>
          ) : null}
        </section>
      ) : null}

      {run && step === "improve" ? (
        <section className="panel-step" aria-label="Build C">
          {attempts.length ? <div className="attempts">{attempts.map((a) => <CAttemptCard key={a.index} attempt={a} run={run} />)}</div> : <EmptyState title="No C was attempted" detail={run.stop_reason || undefined} />}
          <WorkflowGraph run={run} events={tracker.events} status={tracker.job?.state ?? run?.status} mode="abc" />
        </section>
      ) : null}

      {run && step === "result" ? (
        <section className="panel-step" aria-label="Result">
          {done ? (
            <div className={`outcome tone-${outcome?.tone}`}>
              <Frame tone={outcome?.tone === "good" ? "good" : outcome?.tone === "bad" ? "bad" : undefined}>03 Result</Frame>
              <h2 className="outcome-title">{outcome?.title}</h2>
              {run.final_decision ? <p className="outcome-detail">{run.final_decision}</p> : null}
              {run.stop_reason ? <p className="outcome-stop">Stopped because: {run.stop_reason}</p> : null}
              <p className="muted small">{usageLine(run)}</p>
            </div>
          ) : (
            <EmptyState title="The result is not ready" detail="It appears when the comparison finishes." />
          )}
          <PairPlayers run={run} />
        </section>
      ) : null}

      {run ? (
        <section className="details" aria-label="Details">
          <details className="disclosure">
            <summary>Evaluation protocol</summary>
            <div className="disclosure-body">
              <dl className="kv">
                <dt>Rubric</dt>
                <dd className="mono">
                  {run.rubric.version} {run.rubric.sha256 ? `sha256 ${String(run.rubric.sha256).slice(0, 16)}` : ""}
                </dd>
                {run.rubric.verdict_rules ? (
                  <>
                    <dt>Verdict rules</dt>
                    <dd>{run.rubric.verdict_rules}</dd>
                  </>
                ) : null}
                <dt>Reviewer</dt>
                <dd className="mono">{String(run.rubric.reviewer ?? "")}</dd>
                <dt>Selector</dt>
                <dd className="mono">{String(run.rubric.selector ?? "")}</dd>
                <dt>Viewer</dt>
                <dd>
                  {run.context.audience}; {run.context.encounter}
                </dd>
              </dl>
              {run.rubric.whole_dimensions ? (
                <ul className="plain-list small">
                  {Object.entries(run.rubric.whole_dimensions).map(([k, q]) => (
                    <li key={k}>
                      <strong>{k.replace(/_/g, " ")}</strong>: {q}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </details>
          <details className="disclosure">
            <summary>Agent trace</summary>
            <div className="disclosure-body">
              {run.weave_url ? (
                <a className="text-link" href={run.weave_url} target="_blank" rel="noreferrer">
                  Whole comparison in Weave <ExternalIcon size={14} />
                </a>
              ) : (
                <p className="muted">No Weave trace was recorded.</p>
              )}
              {!jobId ? (
                <p className="muted">This comparison was launched from the {run.launched_via === "cli" ? "command line" : run.launched_via}, so it has no job event log. Its stored record is shown above.</p>
              ) : tracker.missing ? (
                <p className="muted">No job exists for this comparison in the API (launched via {run.launched_via}), so there is no event log.</p>
              ) : (
                <EventLog events={tracker.events} />
              )}
            </div>
          </details>
          <details className="disclosure">
            <summary>Technical details</summary>
            <div className="disclosure-body">
              <dl className="kv">
                <dt>Usage</dt>
                <dd>{usageLine(run) || "none recorded"}</dd>
                <dt>Limits</dt>
                <dd className="mono">{JSON.stringify(run.limits)}</dd>
                <dt>Constraints</dt>
                <dd>{run.constraints.length ? run.constraints.join("; ") : "none"}</dd>
                <dt>Mocked stages</dt>
                <dd>{run.mocked_stages.length ? run.mocked_stages.join(", ") : "none"}</dd>
                <dt>Manual steps</dt>
                <dd>{run.manual_interventions.length ? run.manual_interventions.join("; ") : "none"}</dd>
                <dt>Launched via</dt>
                <dd>{run.launched_via}</dd>
              </dl>
            </div>
          </details>
        </section>
      ) : null}
    </div>
  );
}
