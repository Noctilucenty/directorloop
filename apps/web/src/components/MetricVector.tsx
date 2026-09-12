import type { Comparison, EvaluationSummary } from "../api/types";
import { humanize } from "../lib/format";
import { Badge, modeBadge } from "./Badge";

interface Props {
  baseline: EvaluationSummary | null;
  candidate: EvaluationSummary | null;
  comparison: Comparison | null;
}

function Cell({ label, value, badge }: { label: string; value: string; badge: "probe" | "mechanical" }) {
  return (
    <div className="metric">
      <div className="metric-value mono">{value}</div>
      <div className="metric-label">
        {label} <Badge kind={badge} />
      </div>
    </div>
  );
}

export function MetricVector({ baseline, candidate, comparison }: Props) {
  return (
    <section className="panel" aria-label="Measured results">
      <div className="panel-head">
        <h2>Did it improve under the declared tests</h2>
        {candidate ? <Badge kind={modeBadge(candidate.mode)} /> : null}
      </div>
      <div className="metric-grid">
        <div className="metric-col">
          <div className="metric-col-head muted">Baseline</div>
          {baseline ? (
            <>
              <Cell label="Model comprehension probes" value={`${baseline.questions_passed}/${baseline.questions_total}`} badge="probe" />
              <Cell label="Mechanical checks" value={baseline.mechanical_passed ? "pass" : "fail"} badge="mechanical" />
              <Cell label="Protected constraints" value={baseline.constraints_passed ? "pass" : "fail"} badge="mechanical" />
            </>
          ) : (
            <div className="muted">not evaluated</div>
          )}
        </div>
        <div className="metric-col">
          <div className="metric-col-head muted">Candidate</div>
          {candidate ? (
            <>
              <Cell label="Model comprehension probes" value={`${candidate.questions_passed}/${candidate.questions_total}`} badge="probe" />
              <Cell label="Mechanical checks" value={candidate.mechanical_passed ? "pass" : "fail"} badge="mechanical" />
              <Cell label="Protected constraints" value={candidate.constraints_passed ? "pass" : "fail"} badge="mechanical" />
            </>
          ) : (
            <div className="muted">no candidate yet</div>
          )}
        </div>
      </div>
      {candidate ? (
        <div className="muted small">
          {candidate.provider} {candidate.model}, {candidate.trials} trials per question, {candidate.probe_modality.replace(/_/g, " ")}. Model
          samples are not people.
        </div>
      ) : null}
      {comparison ? (
        <div className="regression">
          <div className="regression-row">
            <span className="reg-label ok">Fixed</span>
            <span>{comparison.fixed.length ? comparison.fixed.map(humanize).join(", ") : "none"}</span>
          </div>
          <div className="regression-row">
            <span className="reg-label">Kept</span>
            <span>{comparison.kept.length ? comparison.kept.map(humanize).join(", ") : "none"}</span>
          </div>
          <div className="regression-row">
            <span className={`reg-label ${comparison.regressed.length ? "bad" : ""}`}>Regressed</span>
            <span>{comparison.regressed.length ? comparison.regressed.map(humanize).join(", ") : "none"}</span>
          </div>
          <div className="regression-row">
            <span className="reg-label warn">Still failing</span>
            <span>{comparison.still_failing.length ? comparison.still_failing.map(humanize).join(", ") : "none"}</span>
          </div>
          {!comparison.matched_config ? (
            <div className="callout callout-warn">Configurations differ: {comparison.config_note}</div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
