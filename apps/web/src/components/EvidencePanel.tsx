import type { FailureFinding } from "../api/types";
import { fmtPct, fmtSeconds } from "../lib/format";
import { Badge } from "./Badge";

export function EvidencePanel({ finding }: { finding: FailureFinding | null }) {
  if (!finding) {
    return (
      <section className="panel" aria-label="Top failure">
        <h2>What was wrong</h2>
        <div className="muted">No finding yet.</div>
      </section>
    );
  }
  const ev = finding.evidence;
  return (
    <section className="panel" aria-label="Top failure">
      <div className="panel-head">
        <h2>What was wrong</h2>
        <Badge kind={finding.measurement_type === "mechanical" ? "mechanical" : finding.measurement_type === "human" ? "human" : "probe"} />
      </div>
      <div className="finding-category">{finding.category.replace(/_/g, " ")}</div>
      <div className="finding-meta mono muted">
        severity {fmtPct(finding.severity)} / confidence {fmtPct(finding.confidence)}
        {finding.start_ms !== null && finding.end_ms !== null
          ? ` / ${fmtSeconds(finding.start_ms)} to ${fmtSeconds(finding.end_ms)} (${finding.timestamp_precision} precision)`
          : ""}
      </div>
      <dl className="kv">
        <dt>Observed</dt>
        <dd>{finding.observed}</dd>
        <dt>Inferred cause</dt>
        <dd>{finding.inferred_cause}</dd>
        {finding.alternatives.length ? (
          <>
            <dt>Alternatives</dt>
            <dd>
              <ul className="plain">
                {finding.alternatives.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            </dd>
          </>
        ) : null}
        <dt>Evidence</dt>
        <dd>
          <span className="mono">
            {ev.probe_failures}/{ev.probe_trials}
          </span>{" "}
          probe trials failed on {ev.failed_question_ids.length ? ev.failed_question_ids.map((q) => q.replace(/^q_/, "")).join(", ") : "no questions"}
          {ev.mechanical_check_ids.length ? `; mechanical: ${ev.mechanical_check_ids.join(", ")}` : ""}
          {ev.transcript_mentions_claim !== null
            ? ev.transcript_mentions_claim
              ? "; the transcript mentions the claim"
              : "; the transcript does not mention the claim"
            : ""}
          {ev.coverage_note ? <div className="muted small">{ev.coverage_note}</div> : null}
        </dd>
      </dl>
    </section>
  );
}
