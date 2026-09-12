import { Link } from "react-router-dom";
import type { VersionView } from "../api/types";
import { fmtSeconds } from "../lib/format";

export function VersionTimeline({ versions, selectedId }: { versions: VersionView[]; selectedId: string | null }) {
  return (
    <section className="panel timeline" aria-label="Version timeline">
      <h2>Versions</h2>
      {versions.length === 0 ? <div className="muted">No versions yet.</div> : null}
      <ol className="timeline-list">
        {versions
          .slice()
          .sort((a, b) => a.index - b.index)
          .map((v) => (
            <li key={v.id} className={`timeline-item status-${v.status} ${v.id === selectedId ? "timeline-selected" : ""}`}>
              <Link to={`/versions/${v.id}`} className="timeline-link">
                <span className="timeline-v">V{v.index}</span>
                <span className="timeline-status">{v.status.replace(/_/g, " ")}</span>
                <span className="timeline-meta mono muted">
                  {v.evaluation ? `${v.evaluation.questions_passed}/${v.evaluation.questions_total} probes` : "unevaluated"} / {fmtSeconds(v.duration_ms)}
                </span>
              </Link>
            </li>
          ))}
      </ol>
    </section>
  );
}
