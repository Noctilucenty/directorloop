import type { Job, JobEvent } from "../api/types";
import { isTerminal } from "../api/types";
import type { Transport } from "../hooks/useJob";
import { fmtElapsed } from "../lib/format";

const WORDS: Record<string, string> = {
  STARTED: "Started",
  PLANNING: "Rank experiments",
  RENDERING: "Render variant",
  EVALUATING: "Blind review",
  DECIDING: "Compare and decide",
  LEARNING: "Learn",
  PREPARING: "Preparing both versions",
  AUDITING: "Cold review of A and B",
  AUDITS_DONE: "Both reviews complete",
  ALIGNING: "Matching the two scripts",
  COMPARING_AB: "Comparing A and B",
  COMPARED_AB: "Comparison complete",
  DIRECTING: "Choosing a C to build",
  RENDERING_C: "Rendering C",
  VERIFYING_C: "Checking the render",
  REVIEWING_C: "Fresh review of C",
  JUDGING_C: "Judging C against A and B",
  DECIDED_C: "Decision",
  ANALYZING: "Reading picture and sound",
  COLD_REVIEW: "Cold review",
  DIAGNOSING: "Finding weak moments",
  VERIFYING: "Checking close-ups",
  AUDIT_DONE: "Review complete",
  RESOLVING: "Reading the link",
  DOWNLOADING: "Downloading the video",
  PROBING: "Checking the file",
  REGISTERED: "Ready to judge",
  DONE: "Done",
  FAILED: "Failed",
  CANCELED: "Canceled",
};

export const stageWord = (s: string) => WORDS[s] ?? s.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

/** A flat list of real stages with the time each arrived; pending stages are listed only while the job runs. */
export function StageRail({ job, events, elapsedMs, transport, error, expected }: { job: Job; events: JobEvent[]; elapsedMs: number; transport: Transport; error: string | null; expected: string[] }) {
  const running = !isTerminal(job.state);
  const stageEvents = events.filter(e => e.stage !== "WORKFLOW_STAGE");
  const lastStage = job.stage;
  const stages = [...new Set([...expected, ...stageEvents.map(e => e.stage)])];
  const lastEvent = (stage: string) => [...stageEvents].reverse().find(e => e.stage === stage);
  return (
    <section className="rail" aria-live="polite">
      <div className="rail-head">
        <span className={`rail-state state-${job.state.toLowerCase()}`}>
          <span className="rail-dot" aria-hidden="true" />
          {running ? (job.state === "QUEUED" ? "Queued" : "Working") : job.state === "COMPLETED" ? "Finished" : job.state === "FAILED" ? "Failed" : "Canceled"}
        </span>
        <span className="rail-clock mono">{fmtElapsed(elapsedMs)}</span>
        <span className="muted small">{running ? (transport === "polling" ? "updating every second" : "live") : ""}</span>
      </div>
      <ol className="rail-steps rail-flat">
        {stages.map(stage => {
          const e = lastEvent(stage);
          const current = running && stage === lastStage;
          const complete = Boolean(e) && !current && (job.state === "COMPLETED" || stage !== lastStage);
          const state = current ? "Running" : complete ? "Completed" : job.state === "FAILED" && stage === lastStage ? "Failed" : job.state === "CANCELED" && stage === lastStage ? "Canceled" : running ? "Waiting" : "Not reached";
          return <li key={stage} className={`rail-step${current ? " is-current" : ""}${!e ? " is-pending" : ""}`}>
            <span className="rail-step-name">{stageWord(stage)}</span><span className="rail-step-at">{state}</span>
          </li>;
        })}
      </ol>
      <details className="disclosure"><summary>Execution details</summary><ol className="event-log">{events.map(e => <li key={e.seq}><span>{e.seq}</span><span>{e.stage}</span><span>{e.message}</span></li>)}</ol></details>
      {job.error ? (
        <p className="callout callout-bad" role="alert">
          {job.error}
        </p>
      ) : null}
      {error && running ? <p className="muted small">{error}</p> : null}
    </section>
  );
}
