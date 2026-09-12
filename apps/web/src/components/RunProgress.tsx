import type { JobEvent, JobView } from "../api/types";
import type { Transport } from "../hooks/useJob";
import { fmtClock, fmtMs } from "../lib/format";

const STAGES = [
  "PREFLIGHT",
  "EVALUATING_BASELINE",
  "DIAGNOSING",
  "PLANNING",
  "GENERATING",
  "RENDERING",
  "REEVALUATING",
  "REGRESSION_TESTING",
  "DECIDING",
  "LEARNING",
  "DONE",
];

interface Props {
  job: JobView | null;
  events: JobEvent[];
  transport: Transport;
  error: string | null;
}

export function RunProgress({ job, events, transport, error }: Props) {
  if (!job) {
    return (
      <section className="panel" aria-label="Run progress">
        <h2>Run</h2>
        <div className="muted">Press Improve to run one live iteration, or open a recorded run.</div>
      </section>
    );
  }
  const seen = new Set(events.map((e) => e.stage));
  const currentStage = events.length ? events[events.length - 1].stage : job.stage;
  const timings = job.result?.timings_ms ?? null;
  return (
    <section className="panel" aria-label="Run progress">
      <div className="panel-head">
        <h2>Run {job.id}</h2>
        <span className="muted small mono">
          {job.state} via {transport}
        </span>
      </div>
      <ol className="stage-list">
        {events.map((e) => (
          <li key={e.seq} className={`stage-row ${e.stage === currentStage ? "stage-current" : ""}`}>
            <span className="stage-time mono">{fmtClock(e.ts)}</span>
            <span className="stage-name">{e.stage.replace(/_/g, " ")}</span>
            <span className="stage-msg">{e.message}</span>
          </li>
        ))}
        {!["COMPLETED", "FAILED", "CANCELED", "TIMED_OUT", "NEEDS_REVIEW", "NEEDS_SOURCE_MATERIAL", "NO_GAIN"].includes(job.state)
          ? STAGES.filter((s) => !seen.has(s) && s !== "GENERATING").map((s) => (
              <li key={s} className="stage-row stage-pending">
                <span className="stage-time mono" />
                <span className="stage-name">{s.replace(/_/g, " ")}</span>
                <span className="stage-msg muted">pending</span>
              </li>
            ))
          : null}
      </ol>
      {job.error ? (
        <div className="callout callout-bad" role="alert">
          <strong>Provider or worker error:</strong> <span className="mono">{job.error}</span>
        </div>
      ) : null}
      {error ? <div className="muted small mono">{error}</div> : null}
      {timings ? (
        <div className="timings">
          {Object.entries(timings).map(([k, v]) => (
            <span key={k} className="timing">
              <span className="muted">{k.replace(/_/g, " ")}</span> <span className="mono">{fmtMs(v)}</span>
            </span>
          ))}
        </div>
      ) : null}
    </section>
  );
}
