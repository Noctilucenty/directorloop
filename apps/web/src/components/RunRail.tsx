import type { Job, JobEvent } from "../api/types";
import { isTerminal } from "../api/types";
import type { Transport } from "../hooks/useJob";
import { fmtElapsed } from "../lib/format";
import { stageName } from "../lib/labels";

interface Group {
  key: string;
  title: string;
  phase: "judge" | "attempt" | "end";
  events: JobEvent[];
}

/** Splits a run's events into the judging phase, one group per edit attempt, and the end. */
export function groupRunEvents(events: JobEvent[]): Group[] {
  const groups: Group[] = [];
  let current: Group = { key: "judge", title: "Judge", phase: "judge", events: [] };
  groups.push(current);
  let attempt = 0;
  for (const e of events) {
    if (e.stage === "SELECTING") {
      attempt += 1;
      current = { key: `attempt-${attempt}`, title: `Attempt ${attempt}`, phase: "attempt", events: [] };
      groups.push(current);
    } else if (e.stage === "DONE" || e.stage === "FAILED" || e.stage === "CANCELED") {
      current = { key: "end", title: e.stage === "DONE" ? "Finished" : e.stage === "FAILED" ? "Failed" : "Canceled", phase: "end", events: [] };
      groups.push(current);
    }
    current.events.push(e);
  }
  return groups.filter((g) => g.events.length);
}

export function RunRail({ job, events, elapsedMs, transport, error }: { job: Job; events: JobEvent[]; elapsedMs: number; transport: Transport; error: string | null }) {
  const running = !isTerminal(job.state);
  const startedAt = job.started_at ? Date.parse(job.started_at) : null;
  const groups = groupRunEvents(events);
  const lastSeq = events.length ? events[events.length - 1].seq : -1;
  return (
    <section className="rail" aria-label="Live run" aria-live="polite">
      <div className="rail-head">
        <span className={`rail-state state-${job.state.toLowerCase()}`}>
          <span className="rail-dot" aria-hidden="true" />
          {running ? (job.state === "QUEUED" ? "Queued" : "Working") : job.state === "COMPLETED" ? "Finished" : job.state === "FAILED" ? "Failed" : "Canceled"}
        </span>
        <span className="rail-clock mono">{fmtElapsed(elapsedMs)}</span>
        <span className="muted small">{running ? (transport === "polling" ? "updating every second" : "live") : "all events received"}</span>
      </div>
      {events.length === 0 ? <p className="muted">{running ? "Waiting for the first event." : "No events were recorded for this run."}</p> : null}
      <ol className="rail-groups">
        {groups.map((g) => (
          <li key={g.key} className={`rail-group phase-${g.phase}`}>
            <div className="rail-group-title">{g.title}</div>
            <ol className="rail-steps">
              {g.events.map((e) => {
                const at = startedAt !== null ? Math.max(0, Date.parse(e.ts) - startedAt) : null;
                const current = running && e.seq === lastSeq;
                return (
                  <li key={e.seq} className={`rail-step${current ? " is-current" : ""}${e.stage === "FAILED" ? " is-failed" : ""}`} title={e.message}>
                    <span className="rail-step-name">{stageName(e.stage, g.phase)}</span>
                    <span className="rail-step-at mono">{at === null ? "" : `+${fmtElapsed(at)}`}</span>
                  </li>
                );
              })}
            </ol>
          </li>
        ))}
      </ol>
      {job.error ? (
        <p className="callout callout-bad" role="alert">
          {job.error}
        </p>
      ) : null}
      {error && running ? <p className="muted small">{error}</p> : null}
    </section>
  );
}

export function EventLog({ events }: { events: JobEvent[] }) {
  if (!events.length) return <p className="muted">No job events were recorded.</p>;
  return (
    <ol className="event-log">
      {events.map((e) => (
        <li key={e.seq}>
          <span className="mono muted">{e.seq}</span>
          <span className="mono">{e.stage}</span>
          <span>{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
