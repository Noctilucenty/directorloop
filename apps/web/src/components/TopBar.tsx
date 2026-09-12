import type { JobView, ProjectDetail } from "../api/types";
import { fmtMs, titleCase } from "../lib/format";
import { Badge } from "./Badge";

interface Props {
  project: ProjectDetail | null;
  job: JobView | null;
  elapsedMs: number;
  recorded: boolean;
}

export function TopBar({ project, job, elapsedMs, recorded }: Props) {
  const running = job !== null && !["COMPLETED", "FAILED", "CANCELED", "TIMED_OUT", "NEEDS_REVIEW", "NEEDS_SOURCE_MATERIAL", "NO_GAIN"].includes(job.state);
  const remaining = job ? Math.max(0, job.deadline_ms - elapsedMs) : null;
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="topbar-project">{project?.name ?? "No project loaded"}</div>
        {project ? (
          <div className="topbar-objective">
            <span className="topbar-profile">{titleCase(project.profile)}</span>
            <span className="topbar-sep">/</span>
            <span>{project.brief.objective}</span>
          </div>
        ) : null}
        {project ? (
          <div className="chips" aria-label="Allowed actions">
            {project.brief.allowed_actions.map((a) => (
              <span key={a} className="chip">
                {a.replace(/_/g, " ")}
              </span>
            ))}
            {project.brief.generation_permitted ? <span className="chip chip-muted">generation permitted</span> : <span className="chip chip-muted">no generation</span>}
          </div>
        ) : null}
      </div>
      <div className="topbar-right">
        {job ? (
          <>
            <Badge kind={recorded || job.mode === "recorded" ? "recorded" : "live"} large />
            <div className="timer">
              <div className="timer-value mono">{fmtMs(elapsedMs)}</div>
              <div className="timer-label">{running ? "elapsed" : `finished, ${job.state.toLowerCase().replace(/_/g, " ")}`}</div>
            </div>
            {running && remaining !== null ? (
              <div className={`timer ${remaining < 10000 ? "timer-warn" : ""}`}>
                <div className="timer-value mono">{fmtMs(remaining)}</div>
                <div className="timer-label">to cutoff</div>
              </div>
            ) : null}
          </>
        ) : (
          <div className="timer">
            <div className="timer-label">no run selected</div>
          </div>
        )}
      </div>
    </header>
  );
}
