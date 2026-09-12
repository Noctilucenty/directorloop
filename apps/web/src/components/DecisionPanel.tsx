import type { PromotionDecision } from "../api/types";
import { humanize } from "../lib/format";
import { Badge } from "./Badge";

export function DecisionPanel({ decision }: { decision: PromotionDecision | null }) {
  if (!decision) {
    return (
      <section className="panel" aria-label="Decision">
        <h2>Decision</h2>
        <div className="muted">Pending.</div>
      </section>
    );
  }
  const kind = decision.outcome === "promoted" ? "ok" : decision.outcome === "rejected" ? "bad" : "warn";
  return (
    <section className="panel" aria-label="Decision">
      <div className="panel-head">
        <h2>Decision</h2>
        <Badge kind={kind} label={decision.outcome.replace(/_/g, " ").toUpperCase()} large />
      </div>
      <p className="decision-reason">{decision.reason}</p>
      <ul className="gate-list">
        {decision.hard_gates.map((g) => (
          <li key={g.id} className={`gate-row ${g.passed ? "gate-pass" : "gate-fail"} ${decision.blocking_gate_id === g.id ? "gate-blocking" : ""}`}>
            <span className="gate-dot" aria-hidden="true" />
            <span className="gate-name">{g.id.replace(/_/g, " ")}</span>
            <span className="gate-detail muted">{g.detail}</span>
            {decision.blocking_gate_id === g.id ? <span className="gate-tag">blocked promotion</span> : null}
          </li>
        ))}
      </ul>
      <div className="muted small mono">
        delta {decision.delta_questions >= 0 ? "+" : ""}
        {decision.delta_questions} questions (min {decision.min_improvement_questions}), score {decision.delta_score >= 0 ? "+" : ""}
        {(decision.delta_score * 100).toFixed(0)} pts
        {decision.regressions.length ? `, regressions: ${decision.regressions.map(humanize).join(", ")}` : ""}
      </div>
    </section>
  );
}
